import itertools
from collections import defaultdict
from statistics import median
from typing import Optional
import matplotlib.pyplot as plt
import networkx as nx
import sqlparse
from lineage.exceptions import ConfigError
from sqllineage.core import LineageAnalyzer, LineageResult
from sqllineage.exceptions import SQLLineageException
from pyvis.network import Network
import webbrowser
import base64
from io import BytesIO
from lineage.query_context import QueryContext
from lineage.utils import get_logger
from sqllineage.models import Schema, Table
from tqdm import tqdm

logger = get_logger(__name__)

GRAPH_VISUALIZATION_OPTIONS = """{
            "edges": {
                "color": {
                  "color": "rgba(23,107,215,1)",
                  "highlight": "rgba(23,107,215,1)",
                  "hover": "rgba(23,107,215,1)",
                  "inherit": false
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
                "hover": true,
                "navigationButtons": true,
                "multiselect": true,
                "keyboard": {
                    "enabled": true
                }
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
    UPSTREAM_DIRECTION = 'upstream'
    DOWNSTREAM_DIRECTION = 'downstream'
    BOTH_DIRECTIONS = 'both'
    SELECTED_NODE_COLOR = '#0925C7'
    SELECTED_NODE_TITLE = 'Selected table<br/>'

    def __init__(self, profile_database_name: str, profile_schema_name: str = None, show_isolated_nodes: bool = False,
                 full_table_names: bool = False) -> None:
        self._lineage_graph = nx.DiGraph()
        self._show_isolated_nodes = show_isolated_nodes
        self._profile_database_name = profile_database_name
        self._profile_schema_name = profile_schema_name
        self._show_full_table_name = full_table_names
        self.catalog = defaultdict(lambda: {'volume': [], 'update_times': [], 'last_html': None})

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

    def _update_lineage_graph(self, analyzed_statements: [LineageResult], query_context: QueryContext) -> None:
        database_name = query_context.queried_database
        schema_name = query_context.queried_schema
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

            self._add_nodes_and_edges(sources, targets, query_context)

    def _add_node_to_catalog(self, node: str, query_context: QueryContext) -> None:
        self.catalog[node]['volume'].append(query_context.query_volume)
        self.catalog[node]['update_times'].append(
            query_context.query_time_to_str(query_context.query_time, fmt='%Y-%m-%d %H:%M:%S'))
        self.catalog[node]['last_html'] = query_context.to_html()

    def _add_nodes_and_edges(self, sources: {str}, targets: {str}, query_context: QueryContext) -> None:
        if None in sources:
            sources.remove(None)
        if None in targets:
            targets.remove(None)

        if not sources and not targets:
            return

        if len(sources) > 0 and len(targets) == 0:
            if self._show_isolated_nodes:
                self._lineage_graph.add_nodes_from(sources)
        elif len(targets) > 0 and len(sources) == 0:
            if self._show_isolated_nodes:
                for target_node in targets:
                    self._lineage_graph.add_node(target_node, title=query_context.to_html())
                    self._add_node_to_catalog(target_node, query_context)
        else:
            self._lineage_graph.add_nodes_from(sources)
            for target_node in targets:
                self._lineage_graph.add_node(target_node, title=query_context.to_html())
                self._add_node_to_catalog(target_node, query_context)
            for source, target in itertools.product(sources, targets):
                self._lineage_graph.add_edge(source, target)

    def _rename_node(self, old_node: str, new_node: str) -> None:
        if old_node is None or new_node is None:
            return

        if self._lineage_graph.has_node(old_node):
            # Rename in place instead of copying the entire lineage graph
            nx.relabel_nodes(self._lineage_graph, {old_node: new_node}, copy=False)
            if old_node in self.catalog:
                old_node_attributes = self.catalog[old_node]
                del self.catalog[old_node]
                self.catalog[new_node] = old_node_attributes

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

            if node in self.catalog:
                del self.catalog[node]

    def init_graph_from_query_list(self, queries: [tuple]) -> None:
        logger.debug(f'Loading {len(queries)} queries into the lineage graph')
        for query, query_context in tqdm(queries, desc="Updating lineage graph", colour='green'):
            try:
                analyzed_statements = self._parse_query(query)
            except SQLLineageException as exc:
                logger.debug(f'SQLLineageException was raised while parsing this query -\n{query}\n'
                             f'Error was -\n{exc}.')
                continue

            self._update_lineage_graph(analyzed_statements, query_context)

        logger.debug(f'Finished updating lineage graph!')

    def filter_on_table(self, selected_table: str, direction: str = None, depth: int = None) -> None:
        logger.debug(f'Filtering lineage graph on table - {selected_table}')
        resolved_selected_table_name = self._name_qualification(Table(selected_table), self._profile_database_name,
                                                                self._profile_schema_name)
        logger.debug(f'Qualified table name - {resolved_selected_table_name}')
        if resolved_selected_table_name is None:
            raise ConfigError(f'Could not resolve table name - {selected_table}, please make sure to '
                              f'specify a table name that exists in the database configured in your profiles file.')

        if direction == self.DOWNSTREAM_DIRECTION:
            self._lineage_graph = self._downstream_graph(resolved_selected_table_name, depth)
        elif direction == self.UPSTREAM_DIRECTION:
            self._lineage_graph = self._upstream_graph(resolved_selected_table_name, depth)
        elif direction == self.BOTH_DIRECTIONS:
            downstream_graph = self._downstream_graph(resolved_selected_table_name, depth)
            upstream_graph = self._upstream_graph(resolved_selected_table_name, depth)
            self._lineage_graph = nx.compose(upstream_graph, downstream_graph)
        else:
            raise ConfigError(f'Direction must be one of the following - {self.UPSTREAM_DIRECTION}|'
                              f'{self.DOWNSTREAM_DIRECTION}|{self.BOTH_DIRECTIONS}, '
                              f'Got - {direction} instead.')

        self._update_selected_node_attributes(resolved_selected_table_name)
        logger.debug(f'Finished filtering lineage graph on table - {selected_table}')
        pass

    def _downstream_graph(self, source_node: str, depth: Optional[int]) -> nx.DiGraph:
        logger.debug(f'Building a downstream graph for - {source_node}, depth - {depth}')
        return nx.bfs_tree(G=self._lineage_graph, source=source_node, depth_limit=depth)

    def _upstream_graph(self, target_node: str, depth: Optional[int]) -> nx.DiGraph:
        logger.debug(f'Building an upstream graph for - {target_node}, depth - {depth}')
        reversed_lineage_graph = self._lineage_graph.reverse(copy=True)
        return nx.bfs_tree(G=reversed_lineage_graph, source=target_node, depth_limit=depth).reverse(copy=False)

    def _update_selected_node_attributes(self, selected_node: str) -> None:
        if self._lineage_graph.has_node(selected_node):
            node = self._lineage_graph.nodes[selected_node]
            node_title = node.get('title', '')
            node.update({'color': self.SELECTED_NODE_COLOR,
                         'title': self.SELECTED_NODE_TITLE + node_title})

    def _get_freshness_and_volume_graph_for_node(self, node: str) -> str:
        times = self.catalog[node]['update_times'][-3:]
        volumes = self.catalog[node]['volume'][-3:]
        # plotting a bar chart
        plt.clf()
        plt.bar(times, volumes, width=0.1, color=['blue'])
        plt.xlabel('Time')
        plt.ylabel('Volume')
        plt.title(node)
        fig = plt.gcf()
        tmpfile = BytesIO()
        fig.savefig(tmpfile, format='png')
        encoded = base64.b64encode(tmpfile.getvalue()).decode('utf-8')
        return f"""
        <br/><div style="font-family:arial;color:DarkSlateGrey;font-size:110%;">
                                <strong>
                                    Freshness & volume graph</br>
                                </strong>
                                <img width="400" height="300" src=\'data:image/png;base64,{encoded}\'>
        </div>
        """

    def _enrich_graph_with_monitoring_context(self) -> None:
        # TODO: get only nodes with tag = target
        for node in self._lineage_graph.nodes:
            if node in self.catalog:
                title_html = f"""
                <html>
                    <body>
                        {self.catalog[node]['last_html'] + self._get_freshness_and_volume_graph_for_node(node)}
                    </body>
                </html>
                """

                node_volumes = self.catalog[node]['volume']
                if node_volumes[-1] < median(node_volumes) / 2:
                    self._lineage_graph.nodes[node]['color'] = 'red'
                    title_html = f"""
                        <html>
                            <body>
                                <div style="font-family:arial;color:tomato;font-size:110%;">
                                    <strong>
                                    Warning - last update volume is too low</br></br>
                                    </strong>
                                </div>
                                {self.catalog[node]['last_html'] + self._get_freshness_and_volume_graph_for_node(node)}
                            </body>
                        </html>
                        """
                self._lineage_graph.nodes[node]['title'] = title_html

    def draw_graph(self, should_open_browser: bool = True) -> None:
        self._enrich_graph_with_monitoring_context()
        heading = """
<html style="box-sizing:border-box;font-family:sans-serif;-ms-text-size-adjust:100%;-webkit-text-size-adjust:100%;height:100%;overflow-y:auto;overflow-x:hidden;font-size:16px;">
   <body style="box-sizing:border-box;margin:0;height:auto;min-height:100%;position:relative;">
      <header class="u-align-center-sm u-align-center-xs u-clearfix u-header u-header" id="sec-efee" style="box-sizing:border-box;display:block;position:relative;background-image:none;text-align:center;">
         <div class="u-clearfix u-sheet u-sheet-1" style="box-sizing:border-box;position:relative;width:100%;margin:0 auto;">
            <a href="https://elementary-data.com" class="u-image u-logo u-image-1" data-image-width="650" data-image-height="150" style="box-sizing:border-box;background-color:transparent;-webkit-text-decoration-skip:objects;border-top-width:0;border-left-width:0;border-right-width:0;color:#111111;text-decoration:none;font-size:inherit;font-family:inherit;line-height:inherit;letter-spacing:inherit;text-transform:inherit;font-style:inherit;font-weight:inherit;border:0 none transparent;outline-width:0;margin:18px auto 0 11px;position:relative;object-fit:cover;display:table;vertical-align:middle;background-size:cover;background-position:50% 50%;background-repeat:no-repeat;white-space:nowrap;width:200px;height:46px;">
            <img src=\'data:image/png;base64,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\' class="u-logo-image u-logo-image-1" style="box-sizing:border-box;border-style:none;display:block;width:100%;height:100%;"></a>
            <div class="u-social-icons u-spacing-10 u-social-icons-1" style="box-sizing:border-box;position:relative;display:flex;white-space:nowrap;height:26px;min-height:16px;width:134px;min-width:94px;margin:-46px 20px 16px auto;">
               <a class="u-social-url" target="_blank" title="Star" href="https://github.com/elementary-data/elementary-lineage/stargazers" style="box-sizing:border-box;background-color:transparent;-webkit-text-decoration-skip:objects;border-top-width:0;border-left-width:0;border-right-width:0;color:currentColor;text-decoration:none;font-size:inherit;font-family:inherit;line-height:inherit;letter-spacing:inherit;text-transform:inherit;font-style:inherit;font-weight:inherit;border:0 none transparent;outline-width:0;margin:0;margin-top:0 !important;margin-bottom:0 !important;height:100%;display:inline-block;flex:1;">
                  <span class="u-icon u-social-custom u-social-icon u-text-custom-color-1 u-icon-1" style="box-sizing:border-box;display:flex;line-height:0;border-width:0px;color:#f37474 !important;height:100%;">
                     <svg class="u-svg-link" preserveaspectratio="xMidYMin slice" viewbox="0 -10 511.98685 511" style="box-sizing:border-box;width:100%;height:100%;fill:#f37474;">
                        <use xlink:href="#svg-8a45" style="box-sizing: border-box;"></use>
                     </svg>
                     <svg class="u-svg-content" viewbox="0 -10 511.98685 511" id="svg-8a45" style="box-sizing:border-box;width:0;height:0;fill:#f37474;">
                        <path d="m510.652344 185.902344c-3.351563-10.367188-12.546875-17.730469-23.425782-18.710938l-147.773437-13.417968-58.433594-136.769532c-4.308593-10.023437-14.121093-16.511718-25.023437-16.511718s-20.714844 6.488281-25.023438 16.535156l-58.433594 136.746094-147.796874 13.417968c-10.859376 1.003906-20.03125 8.34375-23.402344 18.710938-3.371094 10.367187-.257813 21.738281 7.957031 28.90625l111.699219 97.960937-32.9375 145.089844c-2.410156 10.667969 1.730468 21.695313 10.582031 28.09375 4.757813 3.4375 10.324219 5.1875 15.9375 5.1875 4.839844 0 9.640625-1.304687 13.949219-3.882813l127.46875-76.183593 127.421875 76.183593c9.324219 5.609376 21.078125 5.097657 29.910156-1.304687 8.855469-6.417969 12.992187-17.449219 10.582031-28.09375l-32.9375-145.089844 111.699219-97.941406c8.214844-7.1875 11.351563-18.539063 7.980469-28.925781zm0 0" fill="currentColor" style="box-sizing: border-box;"></path>
                     </svg>
                  </span>
               </a>
               <a class="u-social-url" target="_blank" title="Slack" href="https://bit.ly/slack-elementary" style="box-sizing:border-box;background-color:transparent;-webkit-text-decoration-skip:objects;border-top-width:0;border-left-width:0;border-right-width:0;color:currentColor;text-decoration:none;font-size:inherit;font-family:inherit;line-height:inherit;letter-spacing:inherit;text-transform:inherit;font-style:inherit;font-weight:inherit;border:0 none transparent;outline-width:0;margin:0;margin-top:0 !important;margin-bottom:0 !important;height:100%;display:inline-block;flex:1;margin-left:10px;">
                  <span class="u-icon u-social-custom u-social-icon u-text-custom-color-1 u-icon-2" style="box-sizing:border-box;display:flex;line-height:0;border-width:0px;color:#f37474 !important;height:100%;">
                     <svg class="u-svg-link" preserveaspectratio="xMidYMin slice" viewbox="0 0 512 512" style="box-sizing:border-box;width:100%;height:100%;fill:#f37474;">
                        <use xlink:href="#svg-8896" style="box-sizing: border-box;"></use>
                     </svg>
                     <svg class="u-svg-content" viewbox="0 0 512 512" id="svg-8896" style="box-sizing:border-box;width:0;height:0;fill:#f37474;">
                        <g style="box-sizing: border-box;">
                           <path d="m467 271h-151c-24.813 0-45 20.187-45 45s20.187 45 45 45h151c24.813 0 45-20.187 45-45s-20.187-45-45-45z" style="box-sizing: border-box;"></path>
                           <path d="m196 151h-151c-24.813 0-45 20.187-45 45s20.187 45 45 45h151c24.813 0 45-20.187 45-45s-20.187-45-45-45z" style="box-sizing: border-box;"></path>
                           <path d="m316 241c24.813 0 45-20.187 45-45v-151c0-24.813-20.187-45-45-45s-45 20.187-45 45v151c0 24.813 20.187 45 45 45z" style="box-sizing: border-box;"></path>
                           <path d="m196 271c-24.813 0-45 20.187-45 45v151c0 24.813 20.187 45 45 45s45-20.187 45-45v-151c0-24.813-20.187-45-45-45z" style="box-sizing: border-box;"></path>
                           <path d="m407 241h45c33.084 0 60-26.916 60-60s-26.916-60-60-60-60 26.916-60 60v45c0 8.284 6.716 15 15 15z" style="box-sizing: border-box;"></path>
                           <path d="m105 271h-45c-33.084 0-60 26.916-60 60s26.916 60 60 60 60-26.916 60-60v-45c0-8.284-6.716-15-15-15z" style="box-sizing: border-box;"></path>
                           <path d="m181 0c-33.084 0-60 26.916-60 60s26.916 60 60 60h45c8.284 0 15-6.716 15-15v-45c0-33.084-26.916-60-60-60z" style="box-sizing: border-box;"></path>
                           <path d="m331 392h-45c-8.284 0-15 6.716-15 15v45c0 33.084 26.916 60 60 60s60-26.916 60-60-26.916-60-60-60z" style="box-sizing: border-box;"></path>
                        </g>
                     </svg>
                  </span>
               </a>
               <a class="u-social-url" target="_blank" title="Github" href="https://github.com/elementary-data/elementary-lineage" style="box-sizing:border-box;background-color:transparent;-webkit-text-decoration-skip:objects;border-top-width:0;border-left-width:0;border-right-width:0;color:currentColor;text-decoration:none;font-size:inherit;font-family:inherit;line-height:inherit;letter-spacing:inherit;text-transform:inherit;font-style:inherit;font-weight:inherit;border:0 none transparent;outline-width:0;margin:0;margin-top:0 !important;margin-bottom:0 !important;height:100%;display:inline-block;flex:1;margin-left:10px;">
                  <span class="u-icon u-social-custom u-social-icon u-text-custom-color-1 u-icon-3" style="box-sizing:border-box;display:flex;line-height:0;border-width:0px;color:#f37474 !important;height:100%;">
                     <svg class="u-svg-link" preserveaspectratio="xMidYMin slice" viewbox="0 0 512 512" style="box-sizing:border-box;width:100%;height:100%;fill:#f37474;">
                        <use xlink:href="#svg-d7b6" style="box-sizing: border-box;"></use>
                     </svg>
                     <svg class="u-svg-content" viewbox="0 0 512 512" x="0px" y="0px" id="svg-d7b6" style="enable-background:new 0 0 512 512;box-sizing:border-box;width:0;height:0;fill:#f37474;">
                        <g style="box-sizing: border-box;">
                           <g style="box-sizing: border-box;">
                              <path d="M255.968,5.329C114.624,5.329,0,120.401,0,262.353c0,113.536,73.344,209.856,175.104,243.872    c12.8,2.368,17.472-5.568,17.472-12.384c0-6.112-0.224-22.272-0.352-43.712c-71.2,15.52-86.24-34.464-86.24-34.464    c-11.616-29.696-28.416-37.6-28.416-37.6c-23.264-15.936,1.728-15.616,1.728-15.616c25.696,1.824,39.2,26.496,39.2,26.496    c22.848,39.264,59.936,27.936,74.528,21.344c2.304-16.608,8.928-27.936,16.256-34.368    c-56.832-6.496-116.608-28.544-116.608-127.008c0-28.064,9.984-51.008,26.368-68.992c-2.656-6.496-11.424-32.64,2.496-68    c0,0,21.504-6.912,70.4,26.336c20.416-5.696,42.304-8.544,64.096-8.64c21.728,0.128,43.648,2.944,64.096,8.672    c48.864-33.248,70.336-26.336,70.336-26.336c13.952,35.392,5.184,61.504,2.56,68c16.416,17.984,26.304,40.928,26.304,68.992    c0,98.72-59.84,120.448-116.864,126.816c9.184,7.936,17.376,23.616,17.376,47.584c0,34.368-0.32,62.08-0.32,70.496    c0,6.88,4.608,14.88,17.6,12.352C438.72,472.145,512,375.857,512,262.353C512,120.401,397.376,5.329,255.968,5.329z" style="box-sizing: border-box;"></path>
                           </g>
                        </g>
                     </svg>
                  </span>
               </a>
               <a class="u-social-url" target="_blank" title="Docs" href="https://docs.elementary-data.com/" style="box-sizing:border-box;background-color:transparent;-webkit-text-decoration-skip:objects;border-top-width:0;border-left-width:0;border-right-width:0;color:currentColor;text-decoration:none;font-size:inherit;font-family:inherit;line-height:inherit;letter-spacing:inherit;text-transform:inherit;font-style:inherit;font-weight:inherit;border:0 none transparent;outline-width:0;margin:0;margin-top:0 !important;margin-bottom:0 !important;height:100%;display:inline-block;flex:1;margin-left:10px;">
                  <span class="u-icon u-social-custom u-social-icon u-text-custom-color-1 u-icon-4" style="box-sizing:border-box;display:flex;line-height:0;border-width:0px;color:#f37474 !important;height:100%;">
                     <svg class="u-svg-link" preserveaspectratio="xMidYMin slice" viewbox="0 0 431.855 431.855" style="box-sizing:border-box;width:100%;height:100%;fill:#f37474;">
                        <use xlink:href="#svg-2c74" style="box-sizing: border-box;"></use>
                     </svg>
                     <svg class="u-svg-content" viewbox="0 0 431.855 431.855" x="0px" y="0px" id="svg-2c74" style="enable-background:new 0 0 431.855 431.855;box-sizing:border-box;width:0;height:0;fill:#f37474;">
                        <g style="box-sizing: border-box;">
                           <path style="fill:currentColor;box-sizing:border-box;" d="M215.936,0C96.722,0,0.008,96.592,0.008,215.814c0,119.336,96.714,216.041,215.927,216.041   c119.279,0,215.911-96.706,215.911-216.041C431.847,96.592,335.214,0,215.936,0z M231.323,335.962   c-5.015,4.463-10.827,6.706-17.411,6.706c-6.812,0-12.754-2.203-17.826-6.617c-5.08-4.406-7.625-10.575-7.625-18.501   c0-7.031,2.463-12.949,7.373-17.745c4.91-4.796,10.933-7.194,18.078-7.194c7.031,0,12.949,2.398,17.753,7.194   c4.796,4.796,7.202,10.713,7.202,17.745C238.858,325.362,236.346,331.5,231.323,335.962z M293.856,180.934   c-3.853,7.145-8.429,13.306-13.737,18.501c-5.292,5.194-14.81,13.924-28.548,26.198c-3.788,3.463-6.836,6.503-9.12,9.12   c-2.284,2.626-3.991,5.023-5.105,7.202c-1.122,2.178-1.983,4.357-2.593,6.535c-0.61,2.17-1.528,5.999-2.772,11.469   c-2.113,11.608-8.754,17.411-19.915,17.411c-5.804,0-10.681-1.894-14.656-5.69c-3.959-3.796-5.934-9.429-5.934-16.907   c0-9.372,1.455-17.493,4.357-24.361c2.886-6.869,6.747-12.892,11.543-18.086c4.804-5.194,11.274-11.356,19.427-18.501   c7.145-6.251,12.307-10.965,15.485-14.144c3.186-3.186,5.861-6.73,8.031-10.632c2.187-3.91,3.26-8.145,3.26-12.721   c0-8.933-3.308-16.46-9.957-22.597c-6.641-6.137-15.209-9.21-25.703-9.21c-12.282,0-21.321,3.097-27.125,9.291   c-5.804,6.194-10.705,15.314-14.729,27.369c-3.804,12.616-11.006,18.923-21.598,18.923c-6.251,0-11.526-2.203-15.826-6.609   c-4.292-4.406-6.438-9.177-6.438-14.314c0-10.6,3.406-21.346,10.21-32.23c6.812-10.884,16.745-19.899,29.807-27.036   c13.054-7.145,28.296-10.722,45.699-10.722c16.184,0,30.466,2.991,42.854,8.966c12.388,5.966,21.963,14.087,28.718,24.361   c6.747,10.266,10.128,21.427,10.128,33.482C299.635,165.473,297.709,173.789,293.856,180.934z"></path>
                        </g>
                     </svg>
                  </span>
               </a>
            </div>
         </div>
      </header>
   </body>
</html>
        """
        # Visualize the graph
        net = Network(height="95%", width="100%", directed=True, heading=heading)
        net.from_nx(self._lineage_graph)
        net.set_options(GRAPH_VISUALIZATION_OPTIONS)

        net.save_graph("elementary_lineage.html")
        if should_open_browser:
            webbrowser.open_new_tab('elementary_lineage.html')