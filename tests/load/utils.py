import contextlib
from importlib import import_module
import codecs
import os
from typing import Any, Iterator, List, Sequence, cast, IO, Tuple
import shutil
from pathlib import Path

import dlt
from dlt.common import json, sleep
from dlt.common.configuration import resolve_configuration
from dlt.common.configuration.container import Container
from dlt.common.configuration.specs.config_section_context import ConfigSectionContext
from dlt.common.destination.reference import DestinationClientDwhConfiguration, DestinationReference, JobClientBase, LoadJob, DestinationClientStagingConfiguration, WithStagingDataset
from dlt.common.data_writers import DataWriter
from dlt.common.schema import TColumnSchema, TTableSchemaColumns, Schema
from dlt.common.storages import SchemaStorage, FileStorage, SchemaStorageConfiguration
from dlt.common.schema.utils import new_table
from dlt.common.storages.load_storage import ParsedLoadJobFileName, LoadStorage
from dlt.common.typing import StrAny
from dlt.common.utils import uniq_id

from dlt.load import Load
from dlt.destinations.sql_client import SqlClientBase
from dlt.destinations.job_client_impl import SqlJobClientBase

from tests.utils import ALL_DESTINATIONS, IMPLEMENTED_DESTINATIONS
from tests.cases import TABLE_UPDATE_COLUMNS_SCHEMA, TABLE_UPDATE, TABLE_ROW_ALL_DATA_TYPES, assert_all_data_types_row

# bucket urls
AWS_BUCKET = dlt.config.get("tests.bucket_url_s3", str)
GCS_BUCKET = dlt.config.get("tests.bucket_url_gs", str)
FILE_BUCKET = dlt.config.get("tests.bucket_url_file", str)
MEMORY_BUCKET = dlt.config.get("tests.memory", str)

ALL_FILESYSTEM_DRIVERS = dlt.config.get("ALL_FILESYSTEM_DRIVERS", list) or ["s3", "gs", "file", "memory"]


# Filter out buckets not in all filesystem drivers
ALL_BUCKETS = [GCS_BUCKET, AWS_BUCKET, FILE_BUCKET, MEMORY_BUCKET]
ALL_BUCKETS = [bucket for bucket in ALL_BUCKETS if bucket.split(':')[0] in ALL_FILESYSTEM_DRIVERS]

ALL_CLIENTS = [f"{name}_client" for name in ALL_DESTINATIONS]


def ALL_CLIENTS_SUBSET(subset: Sequence[str]) -> List[str]:
    return list(set(subset).intersection(ALL_CLIENTS))


def load_table(name: str) -> TTableSchemaColumns:
    with open(f"./tests/load/cases/{name}.json", "rb") as f:
        return cast(TTableSchemaColumns, json.load(f))


def expect_load_file(client: JobClientBase, file_storage: FileStorage, query: str, table_name: str, status = "completed") -> LoadJob:
    file_name = ParsedLoadJobFileName(table_name, uniq_id(), 0, client.capabilities.preferred_loader_file_format).job_id()
    file_storage.save(file_name, query.encode("utf-8"))
    table = Load.get_load_table(client.schema, file_name)
    job = client.start_file_load(table, file_storage.make_full_path(file_name), uniq_id())
    while job.state() == "running":
        sleep(0.5)
    assert job.file_name() == file_name
    assert job.state() ==  status
    return job


def prepare_table(client: JobClientBase, case_name: str = "event_user", table_name: str = "event_user", make_uniq_table: bool = True) -> None:
    client.schema.bump_version()
    client.update_storage_schema()
    user_table = load_table(case_name)[table_name]
    if make_uniq_table:
        user_table_name = table_name + uniq_id()
    else:
        user_table_name = table_name
    client.schema.update_schema(new_table(user_table_name, columns=user_table.values()))
    client.schema.bump_version()
    client.update_storage_schema()
    return user_table_name


def yield_client(
    destination_name: str,
    dataset_name: str = None,
    default_config_values: StrAny = None,
    schema_name: str = "event"
) -> Iterator[SqlJobClientBase]:
    os.environ.pop("DATASET_NAME", None)
    # import destination reference by name
    destination: DestinationReference = import_module(f"dlt.destinations.{destination_name}")
    # create initial config
    dest_config: DestinationClientDwhConfiguration = None
    dest_config = destination.spec()()
    dest_config.dataset_name = dataset_name

    if default_config_values is not None:
        # apply the values to credentials, if dict is provided it will be used as default
        dest_config.credentials = default_config_values
        # also apply to config
        dest_config.update(default_config_values)
    # get event default schema
    storage_config = resolve_configuration(SchemaStorageConfiguration(), explicit_value={
        "schema_volume_path": "tests/common/cases/schemas/rasa"
    })
    schema_storage = SchemaStorage(storage_config)
    schema = schema_storage.load_schema(schema_name)
    # create client and dataset
    client: SqlJobClientBase = None

    # athena requires staging config to be present, so stick this in there here
    if destination_name == "athena":
        staging_config = DestinationClientStagingConfiguration(
            destination_name="fake-stage",
            dataset_name=dest_config.dataset_name,
            default_schema_name=dest_config.default_schema_name,
            bucket_url=AWS_BUCKET
        )
        dest_config.staging_config = staging_config

    # lookup for credentials in the section that is destination name
    with Container().injectable_context(ConfigSectionContext(sections=("destination", destination_name,))):
        with destination.client(schema, dest_config) as client:
            yield client


@contextlib.contextmanager
def cm_yield_client(
    destination_name: str,
    dataset_name: str,
    default_config_values: StrAny = None,
    schema_name: str = "event"
) -> Iterator[SqlJobClientBase]:
    return yield_client(destination_name, dataset_name, default_config_values, schema_name)


def yield_client_with_storage(
    destination_name: str,
    default_config_values: StrAny = None,
    schema_name: str = "event"
) -> Iterator[SqlJobClientBase]:

    # create dataset with random name
    dataset_name = "test_" + uniq_id()

    with cm_yield_client(destination_name, dataset_name, default_config_values, schema_name) as client:
        client.initialize_storage()
        yield client
        # print(dataset_name)
        client.sql_client.drop_dataset()
        if isinstance(client, WithStagingDataset):
            with client.with_staging_dataset():
                if client.is_storage_initialized():
                    client.sql_client.drop_dataset()


def delete_dataset(client: SqlClientBase[Any], normalized_dataset_name: str) -> None:
    try:
        with client.with_alternative_dataset_name(normalized_dataset_name) as client:
            client.drop_dataset()
    except Exception as ex1:
        print(f"Error when deleting temp dataset {normalized_dataset_name}: {str(ex1)}")


@contextlib.contextmanager
def cm_yield_client_with_storage(
    destination_name: str,
    default_config_values: StrAny = None,
    schema_name: str = "event"
) -> Iterator[SqlJobClientBase]:
    return yield_client_with_storage(destination_name, default_config_values, schema_name)


def write_dataset(client: JobClientBase, f: IO[bytes], rows: List[StrAny], columns_schema: TTableSchemaColumns) -> None:
    data_format = DataWriter.data_format_from_file_format(client.capabilities.preferred_loader_file_format)
    # adapt bytes stream to text file format
    if not data_format.is_binary_format and isinstance(f.read(0), bytes):
        f = codecs.getwriter("utf-8")(f)
    writer = DataWriter.from_destination_capabilities(client.capabilities, f)
    # remove None values
    for idx, row in enumerate(rows):
        rows[idx] = {k:v for k, v in row.items() if v is not None}
    writer.write_all(columns_schema, rows)


def prepare_load_package(load_storage: LoadStorage, cases: Sequence[str], write_disposition: str='append') -> Tuple[str, Schema]:
    load_id = uniq_id()
    load_storage.create_temp_load_package(load_id)
    for case in cases:
        path = f"./tests/load/cases/loading/{case}"
        shutil.copy(path, load_storage.storage.make_full_path(f"{load_id}/{LoadStorage.NEW_JOBS_FOLDER}"))
    schema_path = Path("./tests/load/cases/loading/schema.json")
    data = json.loads(schema_path.read_text(encoding='utf8'))
    for name, table in data['tables'].items():
        if name.startswith('_dlt'):
            continue
        table['write_disposition'] = write_disposition
    Path(
        load_storage.storage.make_full_path(load_id)
    ).joinpath(schema_path.name).write_text(json.dumps(data), encoding='utf8')

    schema_update_path = "./tests/load/cases/loading/schema_updates.json"
    shutil.copy(schema_update_path, load_storage.storage.make_full_path(load_id))

    load_storage.commit_temp_load_package(load_id)
    schema = load_storage.load_package_schema(load_id)
    return load_id, schema
