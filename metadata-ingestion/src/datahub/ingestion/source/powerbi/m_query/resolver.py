import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Type, Union, cast

from lark import Tree

from datahub.ingestion.source.powerbi.config import (
    DataPlatformPair,
    PowerBiDashboardSourceReport,
    SupportedDataPlatform,
)
from datahub.ingestion.source.powerbi.m_query import native_sql_parser, tree_function
from datahub.ingestion.source.powerbi.m_query.data_classes import (
    TRACE_POWERBI_MQUERY_PARSER,
    AbstractIdentifierAccessor,
    DataAccessFunctionDetail,
    IdentifierAccessor,
)
from datahub.ingestion.source.powerbi.rest_api_wrapper.data_classes import Table

logger = logging.getLogger(__name__)


@dataclass
class DataPlatformTable:
    name: str
    full_name: str
    datasource_server: str
    data_platform_pair: DataPlatformPair


class AbstractDataPlatformTableCreator(ABC):
    @abstractmethod
    def create_dataplatform_tables(
        self, data_access_func_detail: DataAccessFunctionDetail
    ) -> List[DataPlatformTable]:
        pass

    @abstractmethod
    def get_platform_pair(self) -> DataPlatformPair:
        pass

    @staticmethod
    def get_db_detail_from_argument(
        arg_list: Tree,
    ) -> Tuple[Optional[str], Optional[str]]:
        arguments: List[str] = tree_function.strip_char_from_list(
            values=tree_function.remove_whitespaces_from_list(
                tree_function.token_values(arg_list)
            ),
        )

        if len(arguments) < 2:
            logger.debug(f"Expected minimum 2 arguments, but got {len(arguments)}")
            return None, None

        return arguments[0], arguments[1]


class AbstractDataAccessMQueryResolver(ABC):
    table: Table
    parse_tree: Tree
    parameters: Dict[str, str]
    reporter: PowerBiDashboardSourceReport
    data_access_functions: List[str]

    def __init__(
        self,
        table: Table,
        parse_tree: Tree,
        reporter: PowerBiDashboardSourceReport,
        parameters: Dict[str, str],
    ):
        self.table = table
        self.parse_tree = parse_tree
        self.reporter = reporter
        self.parameters = parameters
        self.data_access_functions = SupportedResolver.get_function_names()

    @abstractmethod
    def resolve_to_data_platform_table_list(self) -> List[DataPlatformTable]:
        pass


class MQueryResolver(AbstractDataAccessMQueryResolver, ABC):
    def get_item_selector_tokens(
        self,
        expression_tree: Tree,
    ) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
        item_selector: Optional[Tree] = tree_function.first_item_selector_func(
            expression_tree
        )
        if item_selector is None:
            logger.debug("Item Selector not found in tree")
            logger.debug(expression_tree.pretty())
            return None, None

        identifier_tree: Optional[Tree] = tree_function.first_identifier_func(
            expression_tree
        )
        if identifier_tree is None:
            logger.debug("Identifier not found in tree")
            logger.debug(item_selector.pretty())
            return None, None

        # remove whitespaces and quotes from token
        tokens: List[str] = tree_function.strip_char_from_list(
            tree_function.remove_whitespaces_from_list(
                tree_function.token_values(
                    cast(Tree, item_selector), parameters=self.parameters
                )
            ),
        )
        identifier: List[str] = tree_function.token_values(
            cast(Tree, identifier_tree)
        )  # type :ignore

        # convert tokens to dict
        iterator = iter(tokens)

        return "".join(identifier), dict(zip(iterator, iterator))

    @staticmethod
    def get_argument_list(invoke_expression: Tree) -> Optional[Tree]:
        argument_list: Optional[Tree] = tree_function.first_arg_list_func(
            invoke_expression
        )
        if argument_list is None:
            logger.debug("First argument-list rule not found in input tree")
            return None

        return argument_list

    def _process_invoke_expression(
        self, invoke_expression: Tree
    ) -> Union[DataAccessFunctionDetail, List[str], None]:
        letter_tree: Tree = invoke_expression.children[0]
        data_access_func: str = tree_function.make_function_name(letter_tree)
        # The invoke function is either DataAccess function like PostgreSQL.Database(<argument-list>) or
        # some other function like Table.AddColumn or Table.Combine and so on
        if data_access_func in self.data_access_functions:
            arg_list: Optional[Tree] = MQueryResolver.get_argument_list(
                invoke_expression
            )
            if arg_list is None:
                self.reporter.report_warning(
                    f"{self.table.full_name}-arg-list",
                    f"Argument list not found for data-access-function {data_access_func}",
                )
                return None

            return DataAccessFunctionDetail(
                arg_list=arg_list,
                data_access_function_name=data_access_func,
                identifier_accessor=None,
            )

        # function is not data-access function, lets process function argument
        first_arg_tree: Optional[Tree] = tree_function.first_arg_list_func(
            invoke_expression
        )

        if first_arg_tree is None:
            logger.debug(
                f"Function invocation without argument in expression = {invoke_expression.pretty()}"
            )
            self.reporter.report_warning(
                f"{self.table.full_name}-variable-statement",
                "Function invocation without argument",
            )
            return None

        flat_arg_list: List[Tree] = tree_function.flat_argument_list(first_arg_tree)
        if len(flat_arg_list) == 0:
            logger.debug("flat_arg_list is zero")
            return None

        first_argument: Tree = flat_arg_list[0]  # take first argument only
        expression: Optional[Tree] = tree_function.first_list_expression_func(
            first_argument
        )

        if TRACE_POWERBI_MQUERY_PARSER:
            logger.debug(f"Extracting token from tree {first_argument.pretty()}")
        else:
            logger.debug(f"Extracting token from tree {first_argument}")
        if expression is None:
            expression = tree_function.first_type_expression_func(first_argument)
            if expression is None:
                logger.debug(
                    f"Either list_expression or type_expression is not found = {invoke_expression.pretty()}"
                )
                self.reporter.report_warning(
                    f"{self.table.full_name}-variable-statement",
                    "Function argument expression is not supported",
                )
                return None

        tokens: List[str] = tree_function.remove_whitespaces_from_list(
            tree_function.token_values(expression)
        )

        logger.debug(f"Tokens in invoke expression are {tokens}")
        return tokens

    def _process_item_selector_expression(
        self, rh_tree: Tree
    ) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
        new_identifier, key_vs_value = self.get_item_selector_tokens(  # type: ignore
            cast(Tree, tree_function.first_expression_func(rh_tree))
        )

        return new_identifier, key_vs_value

    @staticmethod
    def _create_or_update_identifier_accessor(
        identifier_accessor: Optional[IdentifierAccessor],
        new_identifier: str,
        key_vs_value: Dict[str, Any],
    ) -> IdentifierAccessor:
        # It is first identifier_accessor
        if identifier_accessor is None:
            return IdentifierAccessor(
                identifier=new_identifier, items=key_vs_value, next=None
            )

        new_identifier_accessor: IdentifierAccessor = IdentifierAccessor(
            identifier=new_identifier, items=key_vs_value, next=identifier_accessor
        )

        return new_identifier_accessor

    def create_data_access_functional_detail(
        self, identifier: str
    ) -> List[DataAccessFunctionDetail]:
        table_links: List[DataAccessFunctionDetail] = []

        def internal(
            current_identifier: str,
            identifier_accessor: Optional[IdentifierAccessor],
        ) -> None:
            """
            1) Find statement where identifier appear in the left-hand side i.e. identifier  = expression
            2) Check expression is function invocation i.e. invoke_expression or item_selector
            3) if it is function invocation and this function is not the data-access function then take first argument
               i.e. identifier and call the function recursively
            4) if it is item_selector then take identifier and key-value pair,
               add identifier and key-value pair in current_selector and call the function recursively
            5) This recursion will continue till we reach to data-access function and during recursion we will fill
               token_dict dictionary for all item_selector we find during traversal.

            :param current_identifier: variable to look for
            :param identifier_accessor:
            :return: None
            """
            # Grammar of variable_statement is <variable-name> = <expression>
            # Examples: Source = PostgreSql.Database(<arg-list>)
            #           public_order_date = Source{[Schema="public",Item="order_date"]}[Data]
            v_statement: Optional[Tree] = tree_function.get_variable_statement(
                self.parse_tree, current_identifier
            )
            if v_statement is None:
                self.reporter.report_warning(
                    f"{self.table.full_name}-variable-statement",
                    f"output variable ({current_identifier}) statement not found in table expression",
                )
                return None

            # Any expression after "=" sign of variable-statement
            rh_tree: Optional[Tree] = tree_function.first_expression_func(v_statement)
            if rh_tree is None:
                logger.debug("Expression tree not found")
                logger.debug(v_statement.pretty())
                return None

            invoke_expression: Optional[
                Tree
            ] = tree_function.first_invoke_expression_func(rh_tree)

            if invoke_expression is not None:
                result: Union[
                    DataAccessFunctionDetail, List[str], None
                ] = self._process_invoke_expression(invoke_expression)
                if result is None:
                    return None  # No need to process some un-expected grammar found while processing invoke_expression
                if isinstance(result, DataAccessFunctionDetail):
                    result.identifier_accessor = identifier_accessor
                    table_links.append(result)  # Link of a table is completed
                    identifier_accessor = (
                        None  # reset the identifier_accessor for other table
                    )
                    return None
                # Process first argument of the function.
                # The first argument can be a single table argument or list of table.
                # For example Table.Combine({t1,t2},....), here first argument is list of table.
                # Table.AddColumn(t1,....), here first argument is single table.
                for token in cast(List[str], result):
                    internal(token, identifier_accessor)

            else:
                new_identifier, key_vs_value = self._process_item_selector_expression(
                    rh_tree
                )
                if new_identifier is None or key_vs_value is None:
                    logger.debug("Required information not found in rh_tree")
                    return None
                new_identifier_accessor: IdentifierAccessor = (
                    self._create_or_update_identifier_accessor(
                        identifier_accessor, new_identifier, key_vs_value
                    )
                )

                return internal(new_identifier, new_identifier_accessor)

        internal(identifier, None)

        return table_links

    def resolve_to_data_platform_table_list(self) -> List[DataPlatformTable]:
        data_platform_tables: List[DataPlatformTable] = []

        output_variable: Optional[str] = tree_function.get_output_variable(
            self.parse_tree
        )

        if output_variable is None:
            self.reporter.report_warning(
                f"{self.table.full_name}-output-variable",
                "output-variable not found in table expression",
            )
            return data_platform_tables

        table_links: List[
            DataAccessFunctionDetail
        ] = self.create_data_access_functional_detail(output_variable)

        # Each item is data-access function
        for f_detail in table_links:
            supported_resolver = SupportedResolver.get_resolver(
                f_detail.data_access_function_name
            )
            if supported_resolver is None:
                logger.debug(
                    f"Resolver not found for the data-access-function {f_detail.data_access_function_name}"
                )
                self.reporter.report_warning(
                    f"{self.table.full_name}-data-access-function",
                    f"Resolver not found for data-access-function = {f_detail.data_access_function_name}",
                )
                continue

            table_full_name_creator: AbstractDataPlatformTableCreator = (
                supported_resolver.get_table_full_name_creator()()
            )

            data_platform_tables.extend(
                table_full_name_creator.create_dataplatform_tables(f_detail)
            )

        return data_platform_tables


class DefaultTwoStepDataAccessSources(AbstractDataPlatformTableCreator, ABC):
    """
    These are the DataSource for which PowerBI Desktop generates default M-Query of following pattern
        let
            Source = Sql.Database("localhost", "library"),
            dbo_book_issue = Source{[Schema="dbo",Item="book_issue"]}[Data]
        in
            dbo_book_issue
    """

    def two_level_access_pattern(
        self, data_access_func_detail: DataAccessFunctionDetail
    ) -> List[DataPlatformTable]:
        logger.debug(
            f"Processing {self.get_platform_pair().powerbi_data_platform_name} data-access function detail {data_access_func_detail}"
        )

        server, db_name = self.get_db_detail_from_argument(
            data_access_func_detail.arg_list
        )
        if server is None or db_name is None:
            return []  # Return empty list

        schema_name: str = cast(
            IdentifierAccessor, data_access_func_detail.identifier_accessor
        ).items["Schema"]

        table_name: str = cast(
            IdentifierAccessor, data_access_func_detail.identifier_accessor
        ).items["Item"]

        full_table_name: str = f"{db_name}.{schema_name}.{table_name}"

        logger.debug(
            f"Platform({self.get_platform_pair().datahub_data_platform_name}) full_table_name= {full_table_name}"
        )

        return [
            DataPlatformTable(
                name=table_name,
                full_name=full_table_name,
                datasource_server=server,
                data_platform_pair=self.get_platform_pair(),
            )
        ]


class PostgresDataPlatformTableCreator(DefaultTwoStepDataAccessSources):
    def create_dataplatform_tables(
        self, data_access_func_detail: DataAccessFunctionDetail
    ) -> List[DataPlatformTable]:
        return self.two_level_access_pattern(data_access_func_detail)

    def get_platform_pair(self) -> DataPlatformPair:
        return SupportedDataPlatform.POSTGRES_SQL.value


class MSSqlDataPlatformTableCreator(DefaultTwoStepDataAccessSources):
    def get_platform_pair(self) -> DataPlatformPair:
        return SupportedDataPlatform.MS_SQL.value

    def create_dataplatform_tables(
        self, data_access_func_detail: DataAccessFunctionDetail
    ) -> List[DataPlatformTable]:
        dataplatform_tables: List[DataPlatformTable] = []
        arguments: List[str] = tree_function.strip_char_from_list(
            values=tree_function.remove_whitespaces_from_list(
                tree_function.token_values(data_access_func_detail.arg_list)
            ),
        )

        if len(arguments) == 2:
            # It is regular case of MS-SQL
            logger.debug("Handling with regular case")
            return self.two_level_access_pattern(data_access_func_detail)

        if len(arguments) >= 4 and arguments[2] != "Query":
            logger.debug("Unsupported case is found. Second index is not the Query")
            return dataplatform_tables

        db_name: str = arguments[1]

        tables: List[str] = native_sql_parser.get_tables(arguments[3])
        for table in tables:
            schema_and_table: List[str] = table.split(".")
            if len(schema_and_table) == 1:
                # schema name is not present. Default schema name in MS-SQL is dbo
                # https://learn.microsoft.com/en-us/sql/relational-databases/security/authentication-access/ownership-and-user-schema-separation?view=sql-server-ver16
                schema_and_table.insert(0, "dbo")

            dataplatform_tables.append(
                DataPlatformTable(
                    name=schema_and_table[1],
                    full_name=f"{db_name}.{schema_and_table[0]}.{schema_and_table[1]}",
                    datasource_server=arguments[0],
                    data_platform_pair=self.get_platform_pair(),
                )
            )

        logger.debug("MS-SQL full-table-names %s", dataplatform_tables)

        return dataplatform_tables


class OracleDataPlatformTableCreator(AbstractDataPlatformTableCreator):
    def get_platform_pair(self) -> DataPlatformPair:
        return SupportedDataPlatform.ORACLE.value

    @staticmethod
    def _get_server_and_db_name(value: str) -> Tuple[Optional[str], Optional[str]]:
        error_message: str = (
            f"The target argument ({value}) should in the format of <host-name>:<port>/<db-name>["
            ".<domain>]"
        )
        splitter_result: List[str] = value.split("/")
        if len(splitter_result) != 2:
            logger.debug(error_message)
            return None, None

        db_name = splitter_result[1].split(".")[0]

        return tree_function.strip_char_from_list([splitter_result[0]])[0], db_name

    def create_dataplatform_tables(
        self, data_access_func_detail: DataAccessFunctionDetail
    ) -> List[DataPlatformTable]:
        logger.debug(
            f"Processing Oracle data-access function detail {data_access_func_detail}"
        )

        arguments: List[str] = tree_function.remove_whitespaces_from_list(
            tree_function.token_values(data_access_func_detail.arg_list)
        )

        server, db_name = self._get_server_and_db_name(arguments[0])

        if db_name is None or server is None:
            return []

        schema_name: str = cast(
            IdentifierAccessor, data_access_func_detail.identifier_accessor
        ).items["Schema"]

        table_name: str = cast(
            IdentifierAccessor,
            cast(IdentifierAccessor, data_access_func_detail.identifier_accessor).next,
        ).items["Name"]

        return [
            DataPlatformTable(
                name=table_name,
                full_name=f"{db_name}.{schema_name}.{table_name}",
                datasource_server=server,
                data_platform_pair=self.get_platform_pair(),
            )
        ]


class DatabrickDataPlatformTableCreator(AbstractDataPlatformTableCreator):
    def create_dataplatform_tables(
        self, data_access_func_detail: DataAccessFunctionDetail
    ) -> List[DataPlatformTable]:
        logger.debug(
            f"Processing Databrick data-access function detail {data_access_func_detail}"
        )
        value_dict = {}
        temp_accessor: Optional[
            Union[IdentifierAccessor, AbstractIdentifierAccessor]
        ] = data_access_func_detail.identifier_accessor
        while temp_accessor:
            if isinstance(temp_accessor, IdentifierAccessor):
                value_dict[temp_accessor.items["Kind"]] = temp_accessor.items["Name"]
                if temp_accessor.next is not None:
                    temp_accessor = temp_accessor.next
                else:
                    break
            else:
                logger.debug(
                    "expecting instance to be IdentifierAccessor, please check if parsing is done properly"
                )
                return []

        db_name: str = value_dict["Database"]
        schema_name: str = value_dict["Schema"]
        table_name: str = value_dict["Table"]
        server, _ = self.get_db_detail_from_argument(data_access_func_detail.arg_list)

        return [
            DataPlatformTable(
                name=table_name,
                full_name=f"{db_name}.{schema_name}.{table_name}",
                datasource_server=server if server else "",
                data_platform_pair=self.get_platform_pair(),
            )
        ]

    def get_platform_pair(self) -> DataPlatformPair:
        return SupportedDataPlatform.DATABRICK_SQL.value


class DefaultThreeStepDataAccessSources(AbstractDataPlatformTableCreator, ABC):
    def get_datasource_server(
        self, arguments: List[str], data_access_func_detail: DataAccessFunctionDetail
    ) -> str:
        return tree_function.strip_char_from_list([arguments[0]])[0]

    def create_dataplatform_tables(
        self, data_access_func_detail: DataAccessFunctionDetail
    ) -> List[DataPlatformTable]:
        logger.debug(
            f"Processing {self.get_platform_pair().datahub_data_platform_name} function detail {data_access_func_detail}"
        )

        arguments: List[str] = tree_function.remove_whitespaces_from_list(
            tree_function.token_values(data_access_func_detail.arg_list)
        )
        # First is database name
        db_name: str = data_access_func_detail.identifier_accessor.items["Name"]  # type: ignore
        # Second is schema name
        schema_name: str = cast(
            IdentifierAccessor, data_access_func_detail.identifier_accessor.next  # type: ignore
        ).items["Name"]
        # Third is table name
        table_name: str = cast(
            IdentifierAccessor, data_access_func_detail.identifier_accessor.next.next  # type: ignore
        ).items["Name"]

        full_table_name: str = f"{db_name}.{schema_name}.{table_name}"

        logger.debug(
            f"{self.get_platform_pair().datahub_data_platform_name} full-table-name {full_table_name}"
        )

        return [
            DataPlatformTable(
                name=table_name,
                full_name=full_table_name,
                datasource_server=self.get_datasource_server(
                    arguments, data_access_func_detail
                ),
                data_platform_pair=self.get_platform_pair(),
            )
        ]


class SnowflakeDataPlatformTableCreator(DefaultThreeStepDataAccessSources):
    def get_platform_pair(self) -> DataPlatformPair:
        return SupportedDataPlatform.SNOWFLAKE.value


class GoogleBigQueryDataPlatformTableCreator(DefaultThreeStepDataAccessSources):
    def get_platform_pair(self) -> DataPlatformPair:
        return SupportedDataPlatform.GOOGLE_BIGQUERY.value

    def get_datasource_server(
        self, arguments: List[str], data_access_func_detail: DataAccessFunctionDetail
    ) -> str:
        # In Google BigQuery server is project-name
        # condition to silent lint, it is not going to be None
        return (
            data_access_func_detail.identifier_accessor.items["Name"]
            if data_access_func_detail.identifier_accessor is not None
            else str()
        )


class AmazonRedshiftDataPlatformTableCreator(AbstractDataPlatformTableCreator):
    def get_platform_pair(self) -> DataPlatformPair:
        return SupportedDataPlatform.AMAZON_REDSHIFT.value

    def create_dataplatform_tables(
        self, data_access_func_detail: DataAccessFunctionDetail
    ) -> List[DataPlatformTable]:
        logger.debug(
            f"Processing AmazonRedshift data-access function detail {data_access_func_detail}"
        )

        server, db_name = self.get_db_detail_from_argument(
            data_access_func_detail.arg_list
        )
        if db_name is None or server is None:
            return []  # Return empty list

        schema_name: str = cast(
            IdentifierAccessor, data_access_func_detail.identifier_accessor
        ).items["Name"]

        table_name: str = cast(
            IdentifierAccessor,
            cast(IdentifierAccessor, data_access_func_detail.identifier_accessor).next,
        ).items["Name"]

        return [
            DataPlatformTable(
                name=table_name,
                full_name=f"{db_name}.{schema_name}.{table_name}",
                datasource_server=server,
                data_platform_pair=self.get_platform_pair(),
            )
        ]


class NativeQueryDataPlatformTableCreator(AbstractDataPlatformTableCreator):
    SUPPORTED_NATIVE_QUERY_DATA_PLATFORM: dict = {
        SupportedDataPlatform.SNOWFLAKE.value.powerbi_data_platform_name: SupportedDataPlatform.SNOWFLAKE,
        SupportedDataPlatform.AMAZON_REDSHIFT.value.powerbi_data_platform_name: SupportedDataPlatform.AMAZON_REDSHIFT,
    }
    current_data_platform: SupportedDataPlatform = SupportedDataPlatform.SNOWFLAKE

    def get_platform_pair(self) -> DataPlatformPair:
        return self.current_data_platform.value

    @staticmethod
    def is_native_parsing_supported(data_access_function_name: str) -> bool:
        return (
            data_access_function_name
            in NativeQueryDataPlatformTableCreator.SUPPORTED_NATIVE_QUERY_DATA_PLATFORM
        )

    def create_dataplatform_tables(
        self, data_access_func_detail: DataAccessFunctionDetail
    ) -> List[DataPlatformTable]:
        dataplatform_tables: List[DataPlatformTable] = []
        t1: Tree = cast(
            Tree, tree_function.first_arg_list_func(data_access_func_detail.arg_list)
        )
        flat_argument_list: List[Tree] = tree_function.flat_argument_list(t1)

        if len(flat_argument_list) != 2:
            logger.debug(
                f"Expecting 2 argument, actual argument count is {len(flat_argument_list)}"
            )
            logger.debug(f"Flat argument list = {flat_argument_list}")
            return dataplatform_tables
        data_access_tokens: List[str] = tree_function.remove_whitespaces_from_list(
            tree_function.token_values(flat_argument_list[0])
        )

        if not self.is_native_parsing_supported(data_access_tokens[0]):
            logger.debug(
                f"Unsupported native-query data-platform = {data_access_tokens[0]}"
            )
            logger.debug(
                f"NativeQuery is supported only for {self.SUPPORTED_NATIVE_QUERY_DATA_PLATFORM}"
            )

        if len(data_access_tokens[0]) < 3:
            logger.debug(
                f"Server is not available in argument list for data-platform {data_access_tokens[0]}. Returning empty "
                "list"
            )
            return dataplatform_tables

        self.current_data_platform = self.SUPPORTED_NATIVE_QUERY_DATA_PLATFORM[
            data_access_tokens[0]
        ]
        # First argument is the query
        sql_query: str = tree_function.strip_char_from_list(
            values=tree_function.remove_whitespaces_from_list(
                tree_function.token_values(flat_argument_list[1])
            ),
        )[
            0
        ]  # Remove any whitespaces and double quotes character

        for table in native_sql_parser.get_tables(sql_query):
            if len(table.split(".")) != 3:
                logger.debug(
                    f"Skipping table {table} as it is not as per full_table_name format"
                )
                continue

            dataplatform_tables.append(
                DataPlatformTable(
                    name=table.split(".")[2],
                    full_name=table,
                    datasource_server=tree_function.strip_char_from_list(
                        [data_access_tokens[2]]
                    )[0],
                    data_platform_pair=self.get_platform_pair(),
                )
            )

        return dataplatform_tables


class FunctionName(Enum):
    NATIVE_QUERY = "Value.NativeQuery"
    POSTGRESQL_DATA_ACCESS = "PostgreSQL.Database"
    ORACLE_DATA_ACCESS = "Oracle.Database"
    SNOWFLAKE_DATA_ACCESS = "Snowflake.Databases"
    MSSQL_DATA_ACCESS = "Sql.Database"
    DATABRICK_DATA_ACCESS = "Databricks.Catalogs"
    GOOGLE_BIGQUERY_DATA_ACCESS = "GoogleBigQuery.Database"
    AMAZON_REDSHIFT_DATA_ACCESS = "AmazonRedshift.Database"


class SupportedResolver(Enum):
    DATABRICK_QUERY = (
        DatabrickDataPlatformTableCreator,
        FunctionName.DATABRICK_DATA_ACCESS,
    )

    POSTGRES_SQL = (
        PostgresDataPlatformTableCreator,
        FunctionName.POSTGRESQL_DATA_ACCESS,
    )

    ORACLE = (
        OracleDataPlatformTableCreator,
        FunctionName.ORACLE_DATA_ACCESS,
    )

    SNOWFLAKE = (
        SnowflakeDataPlatformTableCreator,
        FunctionName.SNOWFLAKE_DATA_ACCESS,
    )

    MS_SQL = (
        MSSqlDataPlatformTableCreator,
        FunctionName.MSSQL_DATA_ACCESS,
    )

    GOOGLE_BIG_QUERY = (
        GoogleBigQueryDataPlatformTableCreator,
        FunctionName.GOOGLE_BIGQUERY_DATA_ACCESS,
    )

    AMAZON_REDSHIFT = (
        AmazonRedshiftDataPlatformTableCreator,
        FunctionName.AMAZON_REDSHIFT_DATA_ACCESS,
    )

    NATIVE_QUERY = (
        NativeQueryDataPlatformTableCreator,
        FunctionName.NATIVE_QUERY,
    )

    def get_table_full_name_creator(self) -> Type[AbstractDataPlatformTableCreator]:
        return self.value[0]

    def get_function_name(self) -> str:
        return self.value[1].value

    @staticmethod
    def get_function_names() -> List[str]:
        functions: List[str] = []
        for supported_resolver in SupportedResolver:
            functions.append(supported_resolver.get_function_name())

        return functions

    @staticmethod
    def get_resolver(function_name: str) -> Optional["SupportedResolver"]:
        logger.debug(f"Looking for resolver {function_name}")
        for supported_resolver in SupportedResolver:
            if function_name == supported_resolver.get_function_name():
                return supported_resolver
        logger.debug(f"Resolver not found for function_name {function_name}")
        return None
