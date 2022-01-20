import os
from typing import Any, Union
import csv

import snowflake.connector.errors

from exceptions.exceptions import ConfigError
from observability.alerts import Alert
from observability.dbt_runner import DbtRunner
from observability.config import Config
from utils.dbt import extract_credentials_and_data_from_profiles, get_profile_name_from_dbt_project, \
    get_snowflake_client

FILE_DIR = os.path.dirname(__file__)


class DataMonitoring(object):
    DBT_PACKAGE_NAME = 'elementary_observability'
    DBT_PROJECT_PATH = os.path.join(FILE_DIR, 'dbt_project')
    DBT_PROJECT_MODULES_PATH = os.path.join(DBT_PROJECT_PATH, 'dbt_modules', DBT_PACKAGE_NAME)
    DBT_PROJECT_PACKAGES_PATH = os.path.join(DBT_PROJECT_PATH, 'dbt_packages', DBT_PACKAGE_NAME)
    #TODO: maybe use the dbt_project.yml seeds path
    DBT_PROJECT_SEEDS_PATH = os.path.join(DBT_PROJECT_PATH, 'data')

    MONITORING_SCHEMAS_CONFIGURATION = 'monitoring_schemas_configuration'
    MONITORING_TABLES_CONFIGURATION = 'monitoring_tables_configuration'
    MONITORING_COLUMNS_CONFIGURATION = 'monitoring_columns_configuration'

    SELECT_NEW_ALERTS_QUERY = """
        SELECT alert_id, detected_at, database_name, schema_name, table_name, column_name, alert_type, sub_type, 
               alert_description
            FROM ELEMENTARY_ALERTS
            WHERE alert_sent = FALSE;
    """

    UPDATE_SENT_ALERTS = """
        UPDATE ELEMENTARY_ALERTS set alert_sent = TRUE
            WHERE alert_id IN (%s); 
    """

    COUNT_ROWS_QUERY = None

    def __init__(self, config: 'Config', db_connection: Any) -> None:
        self.config = config
        self.dbt_runner = DbtRunner(self.DBT_PROJECT_PATH, self.config.profiles_dir_path)
        self.db_connection = db_connection

    @staticmethod
    def create_data_monitoring(config: 'Config') -> 'DataMonitoring':
        profile_name = get_profile_name_from_dbt_project(DataMonitoring.DBT_PROJECT_PATH)
        credentials, profile_data = extract_credentials_and_data_from_profiles(config.profiles_dir_path, profile_name)
        if credentials.type == 'snowflake':
            snowflake_conn = get_snowflake_client(credentials, server_side_binding=False)
            return SnowflakeDataMonitoring(config, snowflake_conn)
        else:
            raise ConfigError("Unsupported platform")

    def _monitor_schema_changes(self, reload_monitoring_configuration: bool = False):
        pass

    def _dbt_package_exists(self) -> bool:
        return os.path.exists(self.DBT_PROJECT_PACKAGES_PATH) or os.path.exists(self.DBT_PROJECT_MODULES_PATH)

    @staticmethod
    def _alert_on_schema_changes(source_dict: dict) -> Union[bool, None]:
        metadata = source_dict.get('meta', {})
        observability = metadata.get('observability', {})
        return observability.get('alert_on_schema_changes')

    #TODO: maybe break it down a bit to smaller functions
    def _update_configuration(self) -> bool:
        target_csv_dir = self.DBT_PROJECT_SEEDS_PATH
        if not os.path.exists(target_csv_dir):
            os.makedirs(target_csv_dir)

        schema_config_csv_path = os.path.join(target_csv_dir, f'{self.MONITORING_SCHEMAS_CONFIGURATION}.csv')
        table_config_csv_path = os.path.join(target_csv_dir, f'{self.MONITORING_TABLES_CONFIGURATION}.csv')
        column_config_csv_path = os.path.join(target_csv_dir, f'{self.MONITORING_COLUMNS_CONFIGURATION}.csv')

        with open(schema_config_csv_path, 'w') as schema_config_csv, \
             open(table_config_csv_path, 'w') as table_config_csv, \
             open(column_config_csv_path, 'w') as column_config_csv:

            schema_config_csv_writer = csv.DictWriter(schema_config_csv, fieldnames=['database_name',
                                                                                     'schema_name',
                                                                                     'alert_on_schema_changes'])
            schema_config_csv_writer.writeheader()

            table_config_csv_writer = csv.DictWriter(table_config_csv, fieldnames=['database_name',
                                                                                   'schema_name',
                                                                                   'table_name',
                                                                                   'alert_on_schema_changes'])
            table_config_csv_writer.writeheader()

            column_config_csv_writer = csv.DictWriter(column_config_csv, fieldnames=['database_name',
                                                                                     'schema_name',
                                                                                     'table_name',
                                                                                     'column_name',
                                                                                     'column_type',
                                                                                     'alert_on_schema_changes'])
            column_config_csv_writer.writeheader()

            #TODO: use schema if exists instead of source name, use identifier if exists instead of table / column name
            sources = self.config.get_sources()
            for source in sources:
                #TODO: Decide if we need to do extra effort here to bring the relevant db from the profile that
                # is defined in source project
                source_db = source.get('database')
                if source_db is None:
                    continue

                #TODO: validate name represents a schema
                source_name = source.get('name')
                if source_name is None:
                    continue

                alert_on_schema_changes = self._alert_on_schema_changes(source)
                #TODO: should we validate type of 'alert_on_schema_changes' is bool?
                if alert_on_schema_changes is not None:
                    schema_config_csv_writer.writerow({'database_name': source_db,
                                                       'schema_name': source_name,
                                                       'alert_on_schema_changes': alert_on_schema_changes})

                source_tables = source.get('tables', [])
                for source_table in source_tables:
                    table_name = source_table.get('name')
                    if table_name is None:
                        continue

                    alert_on_schema_changes = self._alert_on_schema_changes(source_table)
                    if alert_on_schema_changes is not None:
                        table_config_csv_writer.writerow({'database_name': source_db,
                                                          'schema_name': source_name,
                                                          'table_name': table_name,
                                                          'alert_on_schema_changes': alert_on_schema_changes})

                    source_columns = source_table.get('columns', [])
                    for source_column in source_columns:
                        column_name = source_column.get('name')
                        if column_name is None:
                            continue
                        column_type = source_column.get('meta', {}).get('type')
                        alert_on_schema_changes = self._alert_on_schema_changes(source_column)
                        if alert_on_schema_changes is not None:
                            column_config_csv_writer.writerow({'database_name': source_db,
                                                               'schema_name': source_name,
                                                               'table_name': table_name,
                                                               'column_name': column_name,
                                                               'column_type': column_type,
                                                               'alert_on_schema_changes': alert_on_schema_changes})

        return self.dbt_runner.seed()

    def _run_query(self, query: str, params: tuple = None) -> list:
        pass

    def monitoring_configuration_exists(self) -> bool:
        pass

    def _update_sent_alerts(self, alert_ids) -> None:
        #TODO: check result - in snowflake there should be a column number of rows updated, at least log it without validating the result
        self._run_query(self.UPDATE_SENT_ALERTS, (alert_ids,))

    def _send_alerts_to_slack(self) -> None:
        slack_webhook = self.config.get_slack_notification_webhook()
        if slack_webhook is not None:
            alert_rows = self._run_query(self.SELECT_NEW_ALERTS_QUERY)
            sent_alerts = []
            for alert_row in alert_rows:
                alert = Alert.create_alert_from_row(alert_row)
                alert.send_to_slack(slack_webhook)
                sent_alerts.append(alert.id)

            if len(sent_alerts) > 0:
                self._update_sent_alerts(sent_alerts)

    def run(self, force_update_dbt_packages: bool = False, reload_monitoring_configuration: bool = False,
            dbt_full_refresh: bool = False) -> None:
        if not self._dbt_package_exists() or force_update_dbt_packages:
            if not self.dbt_runner.deps():
                return

        if not self.monitoring_configuration_exists() or reload_monitoring_configuration:
            if not self._update_configuration():
                return

        # Run elementary observability dbt package
        if not self.dbt_runner.snapshot():
            return
        if not self.dbt_runner.run(model=self.DBT_PACKAGE_NAME, full_refresh=dbt_full_refresh):
            return

        self._send_alerts_to_slack()


class SnowflakeDataMonitoring(DataMonitoring):

    COUNT_ROWS_QUERY = """
    SELECT count(*) FROM IDENTIFIER(%s)
    """

    def __init__(self, config: 'Config', db_connection: Any):
        super().__init__(config, db_connection)

    def _run_query(self, query: str, params: tuple = None) -> list:
        with self.db_connection.cursor() as cursor:
            if params is not None:
                cursor.execute(query, params)
            else:
                cursor.execute(query)

            results = cursor.fetchall()
            return results

    def _count_rows_in_table(self, table_name) -> int:
        try:
            results = self._run_query(self.COUNT_ROWS_QUERY,
                                      (table_name,))
            if len(results) == 1:
                return results[0][0]
        except snowflake.connector.errors.ProgrammingError:
            pass

        return 0

    def monitoring_configuration_exists(self) -> bool:
        if self._count_rows_in_table(self.MONITORING_SCHEMAS_CONFIGURATION) > 0:
            return True

        if self._count_rows_in_table(self.MONITORING_TABLES_CONFIGURATION) > 0:
            return True

        if self._count_rows_in_table(self.MONITORING_COLUMNS_CONFIGURATION) > 0:
            return True

        return False

