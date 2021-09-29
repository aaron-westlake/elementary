import itertools
from typing import Optional
from datetime import datetime
import networkx as nx
import sqlparse
from lineage.exceptions import ConfigError
from sqllineage.core import LineageAnalyzer, LineageResult
from sqllineage.exceptions import SQLLineageException
from pyvis.network import Network
import webbrowser
from lineage.utils import get_logger
from sqllineage.models import Schema, Table
from tqdm import tqdm

logger = get_logger(__name__)

GRAPH_VISUALIZATION_OPTIONS = """{
            "edges": {
                "color": {
                    "inherit": true
                },
                "dashes": true,
                "smooth": {
                    "type": "continuous",
                    "forceDirection": "none"
                }
            },
            "layout": {
                "hierarchical": {
                    "enabled": true,
                    "levelSeparation": 485,
                    "nodeSpacing": 100,
                    "treeSpacing": 100,
                    "blockShifting": false,
                    "edgeMinimization": true,
                    "parentCentralization": false,
                    "direction": "LR",
                    "sortMethod": "directed"
                }
            },
            "interaction": {
                "navigationButtons": true,
                "multiselect": true
            },
            "physics": {
                "enabled": false,
                "hierarchicalRepulsion": {
                    "centralGravity": 0
                },
                "minVelocity": 0.75,
                "solver": "hierarchicalRepulsion"
            }
}"""


class LineageGraph(object):
    def __init__(self, profile_database_name: str, profile_schema_name: str = None, show_isolated_nodes: bool = False,
                 full_table_names: bool = False, table: str = None, direction: str = None, depth: int = None) -> None:
        self._lineage_graph = nx.DiGraph()
        self._show_isolated_nodes = show_isolated_nodes
        self._profile_database_name = profile_database_name
        self._profile_schema_name = profile_schema_name
        self._show_full_table_name = full_table_names
        self._table = table
        self._direction = direction
        self._depth = depth

    @staticmethod
    def _parse_query(query: str) -> [LineageResult]:
        parsed_query = sqlparse.parse(query.strip())
        analyzed_statements = [LineageAnalyzer().analyze(statement) for statement in parsed_query
                               if statement.token_first(skip_cm=True, skip_ws=True)]
        return analyzed_statements

    @staticmethod
    def _resolve_table_qualification(table: Table, database_name: str, schema_name: str) -> Table:
        if not table.schema:
            if database_name is not None and schema_name is not None:
                table.schema = Schema(f'{database_name}.{schema_name}')
        else:
            parsed_query_schema_name = str(table.schema)
            if '.' not in parsed_query_schema_name:
                # Resolved schema is either empty or fully qualified with db_name.schema_name
                if database_name is not None:
                    table.schema = Schema(f'{database_name}.{parsed_query_schema_name}')
                else:
                    table.schema = Schema()
        return table

    def _should_ignore_table(self, table: Table) -> bool:
        if self._profile_schema_name is not None:
            if str(table.schema) == str(Schema(f'{self._profile_database_name}.{self._profile_schema_name}')):
                return False
        else:
            if str(Schema(self._profile_database_name)) in str(table.schema):
                return False

        return True

    def _name_qualification(self, table: Table, database_name: str, schema_name: str) -> Optional[str]:
        table = self._resolve_table_qualification(table, database_name, schema_name)

        if self._should_ignore_table(table):
            return None

        if self._show_full_table_name:
            return str(table)

        return str(table).rsplit('.', 1)[-1]

    def _update_lineage_graph(self, analyzed_statements: [LineageResult], database_name: str, schema_name: str,
                              rows_inserted_or_produced: int, end_time: datetime, user_name: str,
                              role_name: str) -> None:
        for analyzed_statement in analyzed_statements:
            # Handle drop tables, if they exist in the statement
            dropped_tables = analyzed_statement.drop
            for dropped_table in dropped_tables:
                dropped_table_name = self._name_qualification(dropped_table, database_name, schema_name)
                self._remove_node(dropped_table_name)

            # Handle rename tables
            renamed_tables = analyzed_statement.rename
            for old_table, new_table in renamed_tables:
                old_table_name = self._name_qualification(old_table, database_name, schema_name)
                new_table_name = self._name_qualification(new_table, database_name, schema_name)
                self._rename_node(old_table_name, new_table_name)

            # sqllineage lib marks CTEs as intermediate tables. Remove CTEs (WITH statements) from the source tables.
            sources = {self._name_qualification(source, database_name, schema_name)
                       for source in analyzed_statement.read - analyzed_statement.intermediate}
            targets = {self._name_qualification(target, database_name, schema_name)
                       for target in analyzed_statement.write}

            self._add_nodes_and_edges(sources, targets, rows_inserted_or_produced, end_time, user_name, role_name)

    def _add_nodes_and_edges(self, sources: {str}, targets: {str}, rows_inserted_or_produced: int,
                             end_time: datetime, user_name: str, role_name: str) -> None:
        if None in sources:
            sources.remove(None)
        if None in targets:
            targets.remove(None)

        if not sources and not targets:
            return

        # TODO: update attributes in any case if node exists (separate it from insertion)
        # TODO: Check pulling the attributes from the catalog so we can update sources as well
        target_attributes = {'title': f'Last update details - <br/> '
                                      f'Time: {end_time}<br/>'
                                      f'Volume: {rows_inserted_or_produced}<br/>'
                                      f'User name: {user_name}<br/>'
                                      f'Role name: {role_name}<br/>'}

        if len(sources) > 0 and len(targets) == 0:
            if self._show_isolated_nodes:
                self._lineage_graph.add_nodes_from(sources)
        elif len(targets) > 0 and len(sources) == 0:
            if self._show_isolated_nodes:
                self._lineage_graph.add_nodes_from(targets, **target_attributes)
        else:
            self._lineage_graph.add_nodes_from(sources)
            self._lineage_graph.add_nodes_from(targets, **target_attributes)
            for source, target in itertools.product(sources, targets):
                self._lineage_graph.add_edge(source, target)

    def _rename_node(self, old_node: str, new_node: str) -> None:
        if old_node is None or new_node is None:
            return

        if self._lineage_graph.has_node(old_node):
            # Rename in place instead of copying the entire lineage graph
            nx.relabel_nodes(self._lineage_graph, {old_node: new_node}, copy=False)

    def _remove_node(self, node: str) -> None:
        # First let's check if the node exists in the graph
        if node is not None and self._lineage_graph.has_node(node):
            node_successors = list(self._lineage_graph.successors(node))
            node_predecessors = list(self._lineage_graph.predecessors(node))

            # networknx's remove_node already takes care of in and out edges
            self._lineage_graph.remove_node(node)

            # Now that we have just deleted the dropped table from the graph, we need to take care of
            # new island nodes.
            if not self._show_isolated_nodes:
                for successor in node_successors:
                    if self._lineage_graph.has_node(successor) and self._lineage_graph.degree(successor) == 0:
                        self._lineage_graph.remove_node(successor)
                for predecessor in node_predecessors:
                    if self._lineage_graph.has_node(predecessor) and self._lineage_graph.degree(predecessor) == 0:
                        self._lineage_graph.remove_node(predecessor)

    def init_graph_from_query_list(self, queries: [tuple]) -> None:
        logger.debug(f'Loading {len(queries)} queries into the lineage graph')
        for query, database_name, schema_name, rows_inserted_or_produced, end_time, user_name, role_name in \
                tqdm(queries, desc="Updating lineage graph", colour='green'):
            try:
                analyzed_statements = self._parse_query(query)
            except SQLLineageException as exc:
                logger.debug(f'SQLLineageException was raised while parsing this query -\n{query}\n'
                             f'Error was -\n{exc}.')
                continue

            self._update_lineage_graph(analyzed_statements, database_name, schema_name, rows_inserted_or_produced,
                                       end_time, user_name, role_name)

        logger.debug(f'Finished updating lineage graph!')

        if self._table is not None:
            logger.debug(f'Filtering on specific table - {self._table}')
            if self._direction == 'upstream':
                logger.debug(f'Starting to build upstream graph for table - {self._table}')
                self._lineage_graph = self._upstream_graph()
                logger.debug(f'Finished building upstream graph for table - {self._table}')
            elif self._direction == 'downstream':
                logger.debug(f'Starting to build downstream graph for table - {self._table}')
                self._lineage_graph = self._downstream_graph()
                logger.debug(f'Finished building downstream graph for table - {self._table}')
            elif self._direction == 'both':
                logger.debug(f'Starting to build upstream & downstream graph for table - {self._table}')
                upstream_graph = self._upstream_graph()
                logger.debug(f'Finished building upstream graph for table - {self._table}')
                downstream_graph = self._downstream_graph()
                logger.debug(f'Finished building downstream graph for table - {self._table}')
                self._lineage_graph = nx.compose(upstream_graph, downstream_graph)
                logger.debug(f'Finished composing upstream & downstream graphs for table - {self._table}')
            else:
                ConfigError('Direction for table filter must be - downstream/upstream/both.')

            title = self._lineage_graph.nodes[self._table].get('title', '')
            self._lineage_graph.nodes[self._table].update({'color': '#9FEEDE', 'title': 'SELECTED NODE' + title})

    def _downstream_graph(self):
        return nx.bfs_tree(G=self._lineage_graph, source=self._table, depth_limit=self._depth)

    def _upstream_graph(self):
        reversed_lineage_graph = self._lineage_graph.reverse(copy=True)
        return nx.bfs_tree(G=reversed_lineage_graph, source=self._table, depth_limit=self._depth).reverse(copy=True)

    def draw_graph(self, should_open_browser: bool = True) -> None:
        # Visualize the graph
        net = Network(height="100%", width="100%", directed=True)
        net.from_nx(self._lineage_graph)
        net.set_options(GRAPH_VISUALIZATION_OPTIONS)

        net.save_graph("elementary_lineage.html")
        if should_open_browser:
            webbrowser.open_new_tab('elementary_lineage.html')