import posixpath
import threading
import os
from types import TracebackType
from typing import ClassVar, List, Sequence, Type, Iterable, cast, Set
from fsspec import AbstractFileSystem

from dlt.common.schema import Schema, TTableSchema
from dlt.common.schema.typing import TWriteDisposition, LOADS_TABLE_NAME
from dlt.common.storages import FileStorage
from dlt.common.destination import DestinationCapabilitiesContext
from dlt.common.destination.reference import NewLoadJob, TLoadJobState, LoadJob, JobClientBase, FollowupJob
from dlt.destinations.job_impl import EmptyLoadJob
from dlt.destinations.filesystem import capabilities
from dlt.destinations.filesystem.configuration import FilesystemClientConfiguration
from dlt.destinations.filesystem.filesystem_client import client_from_config
from dlt.common.storages import LoadStorage
from dlt.destinations.job_impl import NewLoadJobImpl
from dlt.destinations.job_impl import NewReferenceJob
from dlt.destinations import path_utils
from dlt.destinations.exceptions import CantExtractTablePrefix

class LoadFilesystemJob(LoadJob):
    def __init__(
            self,
            local_path: str,
            dataset_path: str,
            *,
            config: FilesystemClientConfiguration,
            schema_name: str,
            load_id: str
    ) -> None:
        file_name = FileStorage.get_file_name_from_file_path(local_path)
        self.config = config
        self.dataset_path = dataset_path
        self.destination_file_name = LoadFilesystemJob.make_destination_filename(config.layout, file_name, schema_name, load_id)

        super().__init__(file_name)
        fs_client, _ = client_from_config(config)
        self.destination_file_name = LoadFilesystemJob.make_destination_filename(config.layout, file_name, schema_name, load_id)
        fs_client.put_file(local_path, self.make_remote_path())

    @staticmethod
    def make_destination_filename(layout: str, file_name: str, schema_name: str, load_id: str) -> str:
        job_info = LoadStorage.parse_job_file_name(file_name)
        return path_utils.create_path(layout,
                                      schema_name=schema_name,
                                      table_name=job_info.table_name,
                                      load_id=load_id,
                                      file_id=job_info.file_id,
                                      ext=job_info.file_format)

    def make_remote_path(self) -> str:
        return f"{self.config.protocol}://{posixpath.join(self.dataset_path, self.destination_file_name)}"

    def state(self) -> TLoadJobState:
        return "completed"

    def exception(self) -> str:
        raise NotImplementedError()


class FollowupFilesystemJob(FollowupJob, LoadFilesystemJob):
    def create_followup_jobs(self, next_state: str) -> List[NewLoadJob]:
        jobs = super().create_followup_jobs(next_state)
        if next_state == "completed":
            ref_job = NewReferenceJob(file_name=self.file_name(), status="running", remote_path=self.make_remote_path())
            jobs.append(ref_job)
        return jobs


class FilesystemClient(JobClientBase):
    """filesystem client storing jobs in memory"""

    capabilities: ClassVar[DestinationCapabilitiesContext] = capabilities()
    fs_client: AbstractFileSystem
    fs_path: str

    def __init__(self, schema: Schema, config: FilesystemClientConfiguration) -> None:
        super().__init__(schema, config)
        self.fs_client, self.fs_path = client_from_config(config)
        self.config: FilesystemClientConfiguration = config

    @property
    def dataset_path(self) -> str:
        return posixpath.join(self.fs_path, self.config.dataset_name)

    def initialize_storage(self, truncate_tables: Iterable[str] = None) -> None:
        # clean up existing files for tables selected for truncating
        if truncate_tables and self.fs_client.isdir(self.dataset_path):

            # collect files
            all_files = []
            for basedir, _dirs, files  in self.fs_client.walk(self.dataset_path, detail=False, refresh=True):
                all_files += [posixpath.join(basedir, file) for file in files]

            for table in truncate_tables:
                table_prefix = path_utils.get_table_prefix(self.config.layout, self.schema.name, table)
                search_prefix = posixpath.join(self.dataset_path, table_prefix)
                for item in all_files:
                    # NOTE: glob implementation in fsspec does not look thread safe, way better is to use ls and then filter
                    if item.startswith(search_prefix):
                        # NOTE: deleting in chunks on s3 does not raise on access denied, file non existing and probably other errors
                        self.fs_client.rm_file(item)


        # create destination dirs for all tables
        dirs_to_create: Set[str] = set()
        for tschema in self.schema.tables.values():
            table_prefix = path_utils.get_table_prefix(self.config.layout, self.schema.name, tschema["name"])
            destination_dir = posixpath.join(self.dataset_path, table_prefix)
            dirs_to_create.add(os.path.dirname(destination_dir))

        for dir in dirs_to_create:
            self.fs_client.makedirs(dir, exist_ok=True)

    def is_storage_initialized(self) -> bool:
        return self.fs_client.isdir(self.dataset_path)  # type: ignore[no-any-return]

    def start_file_load(self, table: TTableSchema, file_path: str, load_id: str) -> LoadJob:
        cls = FollowupFilesystemJob if self.config.as_staging else LoadFilesystemJob
        return cls(
            file_path,
            self.dataset_path,
            config=self.config,
            schema_name=self.schema.name,
            load_id=load_id
        )

    def restore_file_load(self, file_path: str) -> LoadJob:
        return EmptyLoadJob.from_file_path(file_path, "completed")

    def complete_load(self, load_id: str) -> None:
        schema_name = self.schema.name
        table_name = LOADS_TABLE_NAME
        file_name = f"{schema_name}.{table_name}.{load_id}"
        self.fs_client.touch(posixpath.join(self.dataset_path, file_name))

    def __enter__(self) -> "FilesystemClient":
        return self

    def __exit__(self, exc_type: Type[BaseException], exc_val: BaseException, exc_tb: TracebackType) -> None:
        pass
