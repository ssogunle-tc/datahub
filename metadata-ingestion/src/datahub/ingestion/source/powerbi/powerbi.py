#########################################################
#
# Meta Data Ingestion From the Power BI Source
#
#########################################################

import logging
from typing import Iterable, List, Optional, Tuple, Union, cast

import datahub.emitter.mce_builder as builder
import datahub.ingestion.source.powerbi.rest_api_wrapper.data_classes as powerbi_data_classes
from datahub.configuration.source_common import DEFAULT_ENV
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.mcp_builder import gen_containers
from datahub.ingestion.api.common import PipelineContext
from datahub.ingestion.api.decorators import (
    SourceCapability,
    SupportStatus,
    capability,
    config_class,
    platform_name,
    support_status,
)
from datahub.ingestion.api.source import SourceReport
from datahub.ingestion.api.workunit import MetadataWorkUnit
from datahub.ingestion.source.common.subtypes import BIContainerSubTypes
from datahub.ingestion.source.powerbi.config import (
    Constant,
    PlatformDetail,
    PowerBiDashboardSourceConfig,
    PowerBiDashboardSourceReport,
)
from datahub.ingestion.source.powerbi.m_query import parser, resolver
from datahub.ingestion.source.powerbi.rest_api_wrapper.powerbi_api import PowerBiAPI
from datahub.ingestion.source.state.sql_common_state import (
    BaseSQLAlchemyCheckpointState,
)
from datahub.ingestion.source.state.stale_entity_removal_handler import (
    StaleEntityRemovalHandler,
)
from datahub.ingestion.source.state.stateful_ingestion_base import (
    StatefulIngestionSourceBase,
)
from datahub.metadata.com.linkedin.pegasus2avro.common import ChangeAuditStamps
from datahub.metadata.schema_classes import (
    BrowsePathsClass,
    ChangeTypeClass,
    ChartInfoClass,
    ChartKeyClass,
    ContainerClass,
    CorpUserKeyClass,
    DashboardInfoClass,
    DashboardKeyClass,
    DatasetLineageTypeClass,
    DatasetPropertiesClass,
    GlobalTagsClass,
    OwnerClass,
    OwnershipClass,
    OwnershipTypeClass,
    StatusClass,
    SubTypesClass,
    TagAssociationClass,
    UpstreamClass,
    UpstreamLineageClass,
)
from datahub.utilities.dedup_list import deduplicate_list
from datahub.utilities.source_helpers import (
    auto_stale_entity_removal,
    auto_status_aspect,
    auto_workunit_reporter,
)

# Logger instance
logger = logging.getLogger(__name__)


class Mapper:
    """
    Transform PowerBi concepts Dashboard, Dataset and Tile to DataHub concepts Dashboard, Dataset and Chart
    """

    class EquableMetadataWorkUnit(MetadataWorkUnit):
        """
        We can add EquableMetadataWorkUnit to set.
        This will avoid passing same MetadataWorkUnit to DataHub Ingestion framework.
        """

        def __eq__(self, instance):
            return self.id == instance.id

        def __hash__(self):
            return id(self.id)

    def __init__(
        self,
        config: PowerBiDashboardSourceConfig,
        reporter: PowerBiDashboardSourceReport,
    ):
        self.__config = config
        self.__reporter = reporter

    @staticmethod
    def urn_to_lowercase(value: str, flag: bool) -> str:
        if flag is True:
            return value.lower()

        return value

    def lineage_urn_to_lowercase(self, value):
        return Mapper.urn_to_lowercase(
            value, self.__config.convert_lineage_urns_to_lowercase
        )

    def assets_urn_to_lowercase(self, value):
        return Mapper.urn_to_lowercase(value, self.__config.convert_urns_to_lowercase)

    def new_mcp(
        self,
        entity_type,
        entity_urn,
        aspect_name,
        aspect,
        change_type=ChangeTypeClass.UPSERT,
    ):
        """
        Create MCP
        """
        return MetadataChangeProposalWrapper(
            entityType=entity_type,
            changeType=change_type,
            entityUrn=entity_urn,
            aspectName=aspect_name,
            aspect=aspect,
        )

    def _to_work_unit(
        self, mcp: MetadataChangeProposalWrapper
    ) -> EquableMetadataWorkUnit:
        return Mapper.EquableMetadataWorkUnit(
            id="{PLATFORM}-{ENTITY_URN}-{ASPECT_NAME}".format(
                PLATFORM=self.__config.platform_name,
                ENTITY_URN=mcp.entityUrn,
                ASPECT_NAME=mcp.aspectName,
            ),
            mcp=mcp,
        )

    def extract_lineage(
        self, table: powerbi_data_classes.Table, ds_urn: str
    ) -> List[MetadataChangeProposalWrapper]:
        mcps: List[MetadataChangeProposalWrapper] = []

        # table.dataset should always be set, but we check it just in case.
        parameters = table.dataset.parameters if table.dataset else {}

        upstreams: List[UpstreamClass] = []
        upstream_tables: List[resolver.DataPlatformTable] = parser.get_upstream_tables(
            table, self.__reporter, parameters=parameters
        )

        for upstream_table in upstream_tables:
            if (
                upstream_table.data_platform_pair.powerbi_data_platform_name
                not in self.__config.dataset_type_mapping.keys()
            ):
                logger.debug(
                    f"Skipping upstream table for {ds_urn}. The platform {upstream_table.data_platform_pair.powerbi_data_platform_name} is not part of dataset_type_mapping",
                )
                continue

            platform: Union[str, PlatformDetail] = self.__config.dataset_type_mapping[
                upstream_table.data_platform_pair.powerbi_data_platform_name
            ]

            platform_name: str = (
                upstream_table.data_platform_pair.datahub_data_platform_name
            )

            platform_instance_name: Optional[str] = None
            platform_env: str = DEFAULT_ENV
            # Determine if PlatformDetail is provided
            if isinstance(platform, PlatformDetail):
                platform_instance_name = cast(
                    PlatformDetail, platform
                ).platform_instance
                platform_env = cast(PlatformDetail, platform).env

            upstream_urn = builder.make_dataset_urn_with_platform_instance(
                platform=platform_name,
                platform_instance=platform_instance_name,
                env=platform_env,
                name=self.lineage_urn_to_lowercase(upstream_table.full_name),
            )

            upstream_table_class = UpstreamClass(
                upstream_urn,
                DatasetLineageTypeClass.TRANSFORMED,
            )
            upstreams.append(upstream_table_class)

        if len(upstreams) > 0:
            upstream_lineage = UpstreamLineageClass(upstreams=upstreams)
            logger.debug(
                f"DataSet Lineage = {ds_urn} and its lineage = {upstream_lineage}"
            )
            mcp = MetadataChangeProposalWrapper(
                entityType=Constant.DATASET,
                changeType=ChangeTypeClass.UPSERT,
                entityUrn=ds_urn,
                aspect=upstream_lineage,
            )
            mcps.append(mcp)

        return mcps

    def to_datahub_dataset(
        self,
        dataset: Optional[powerbi_data_classes.PowerBIDataset],
        workspace: powerbi_data_classes.Workspace,
    ) -> List[MetadataChangeProposalWrapper]:
        """
        Map PowerBi dataset to datahub dataset. Here we are mapping each table of PowerBi Dataset to Datahub dataset.
        In PowerBi Tile would be having single dataset, However corresponding Datahub's chart might have many input sources.
        """

        dataset_mcps: List[MetadataChangeProposalWrapper] = []
        if dataset is None:
            return dataset_mcps

        logger.debug(
            f"Mapping dataset={dataset.name}(id={dataset.id}) to datahub dataset"
        )

        for table in dataset.tables:
            # Create a URN for dataset
            ds_urn = builder.make_dataset_urn(
                platform=self.__config.platform_name,
                name=self.assets_urn_to_lowercase(table.full_name),
                env=self.__config.env,
            )

            logger.debug(f"{Constant.Dataset_URN}={ds_urn}")
            # Create datasetProperties mcp
            ds_properties = DatasetPropertiesClass(
                name=table.name,
                description=dataset.description,
            )

            info_mcp = self.new_mcp(
                entity_type=Constant.DATASET,
                entity_urn=ds_urn,
                aspect_name=Constant.DATASET_PROPERTIES,
                aspect=ds_properties,
            )

            # Remove status mcp
            status_mcp = self.new_mcp(
                entity_type=Constant.DATASET,
                entity_urn=ds_urn,
                aspect_name=Constant.STATUS,
                aspect=StatusClass(removed=False),
            )
            dataset_mcps.extend([info_mcp, status_mcp])

            if self.__config.extract_lineage is True:
                dataset_mcps.extend(self.extract_lineage(table, ds_urn))

            self.append_container_mcp(
                dataset_mcps,
                workspace,
                ds_urn,
            )

            self.append_tag_mcp(
                dataset_mcps,
                ds_urn,
                Constant.DATASET,
                dataset.tags,
            )

        return dataset_mcps

    @staticmethod
    def transform_tags(tags: List[str]) -> GlobalTagsClass:
        return GlobalTagsClass(
            tags=[
                TagAssociationClass(builder.make_tag_urn(tag_to_add))
                for tag_to_add in tags
            ]
        )

    def to_datahub_chart_mcp(
        self,
        tile: powerbi_data_classes.Tile,
        ds_mcps: List[MetadataChangeProposalWrapper],
        workspace: powerbi_data_classes.Workspace,
    ) -> List[MetadataChangeProposalWrapper]:
        """
        Map PowerBi tile to datahub chart
        """
        logger.info(f"Converting tile {tile.title}(id={tile.id}) to chart")
        # Create a URN for chart
        chart_urn = builder.make_chart_urn(
            self.__config.platform_name, tile.get_urn_part()
        )

        logger.info(f"{Constant.CHART_URN}={chart_urn}")

        ds_input: List[str] = self.to_urn_set(ds_mcps)

        def tile_custom_properties(tile: powerbi_data_classes.Tile) -> dict:
            custom_properties: dict = {
                Constant.CREATED_FROM: tile.createdFrom.value,
            }

            if tile.dataset_id is not None:
                custom_properties[Constant.DATASET_ID] = tile.dataset_id

            if tile.dataset is not None and tile.dataset.webUrl is not None:
                custom_properties[Constant.DATASET_WEB_URL] = tile.dataset.webUrl

            if tile.report is not None and tile.report.id is not None:
                custom_properties[Constant.REPORT_ID] = tile.report.id

            return custom_properties

        # Create chartInfo mcp
        # Set chartUrl only if tile is created from Report
        chart_info_instance = ChartInfoClass(
            title=tile.title or "",
            description=tile.title or "",
            lastModified=ChangeAuditStamps(),
            inputs=ds_input,
            externalUrl=tile.report.webUrl if tile.report else None,
            customProperties=tile_custom_properties(tile),
        )

        info_mcp = self.new_mcp(
            entity_type=Constant.CHART,
            entity_urn=chart_urn,
            aspect_name=Constant.CHART_INFO,
            aspect=chart_info_instance,
        )

        # removed status mcp
        status_mcp = self.new_mcp(
            entity_type=Constant.CHART,
            entity_urn=chart_urn,
            aspect_name=Constant.STATUS,
            aspect=StatusClass(removed=False),
        )

        # ChartKey status
        chart_key_instance = ChartKeyClass(
            dashboardTool=self.__config.platform_name,
            chartId=Constant.CHART_ID.format(tile.id),
        )

        chart_key_mcp = self.new_mcp(
            entity_type=Constant.CHART,
            entity_urn=chart_urn,
            aspect_name=Constant.CHART_KEY,
            aspect=chart_key_instance,
        )
        browse_path = BrowsePathsClass(paths=["/powerbi/{}".format(workspace.name)])
        browse_path_mcp = self.new_mcp(
            entity_type=Constant.CHART,
            entity_urn=chart_urn,
            aspect_name=Constant.BROWSERPATH,
            aspect=browse_path,
        )
        result_mcps = [info_mcp, status_mcp, chart_key_mcp, browse_path_mcp]

        self.append_container_mcp(
            result_mcps,
            workspace,
            chart_urn,
        )

        return result_mcps

    # written in this style to fix linter error
    def to_urn_set(self, mcps: List[MetadataChangeProposalWrapper]) -> List[str]:
        return deduplicate_list(
            [
                mcp.entityUrn
                for mcp in mcps
                if mcp is not None and mcp.entityUrn is not None
            ]
        )

    def to_datahub_dashboard_mcp(
        self,
        dashboard: powerbi_data_classes.Dashboard,
        workspace: powerbi_data_classes.Workspace,
        chart_mcps: List[MetadataChangeProposalWrapper],
        user_mcps: List[MetadataChangeProposalWrapper],
    ) -> List[MetadataChangeProposalWrapper]:
        """
        Map PowerBi dashboard to Datahub dashboard
        """

        dashboard_urn = builder.make_dashboard_urn(
            self.__config.platform_name, dashboard.get_urn_part()
        )

        chart_urn_list: List[str] = self.to_urn_set(chart_mcps)
        user_urn_list: List[str] = self.to_urn_set(user_mcps)

        def chart_custom_properties(dashboard: powerbi_data_classes.Dashboard) -> dict:
            return {
                Constant.CHART_COUNT: str(len(dashboard.tiles)),
                Constant.WORKSPACE_NAME: dashboard.workspace_name,
                Constant.WORKSPACE_ID: dashboard.workspace_id,
            }

        # DashboardInfo mcp
        dashboard_info_cls = DashboardInfoClass(
            description=dashboard.description,
            title=dashboard.displayName or "",
            charts=chart_urn_list,
            lastModified=ChangeAuditStamps(),
            dashboardUrl=dashboard.webUrl,
            customProperties={**chart_custom_properties(dashboard)},
        )

        info_mcp = self.new_mcp(
            entity_type=Constant.DASHBOARD,
            entity_urn=dashboard_urn,
            aspect_name=Constant.DASHBOARD_INFO,
            aspect=dashboard_info_cls,
        )

        # removed status mcp
        removed_status_mcp = self.new_mcp(
            entity_type=Constant.DASHBOARD,
            entity_urn=dashboard_urn,
            aspect_name=Constant.STATUS,
            aspect=StatusClass(removed=False),
        )

        # dashboardKey mcp
        dashboard_key_cls = DashboardKeyClass(
            dashboardTool=self.__config.platform_name,
            dashboardId=Constant.DASHBOARD_ID.format(dashboard.id),
        )

        # Dashboard key
        dashboard_key_mcp = self.new_mcp(
            entity_type=Constant.DASHBOARD,
            entity_urn=dashboard_urn,
            aspect_name=Constant.DASHBOARD_KEY,
            aspect=dashboard_key_cls,
        )

        # Dashboard Ownership
        owners = [
            OwnerClass(owner=user_urn, type=OwnershipTypeClass.NONE)
            for user_urn in user_urn_list
            if user_urn is not None
        ]

        owner_mcp = None
        if len(owners) > 0:
            # Dashboard owner MCP
            ownership = OwnershipClass(owners=owners)
            owner_mcp = self.new_mcp(
                entity_type=Constant.DASHBOARD,
                entity_urn=dashboard_urn,
                aspect_name=Constant.OWNERSHIP,
                aspect=ownership,
            )

        # Dashboard browsePaths
        browse_path = BrowsePathsClass(
            paths=[f"/{Constant.PLATFORM_NAME}/{dashboard.workspace_name}"]
        )
        browse_path_mcp = self.new_mcp(
            entity_type=Constant.DASHBOARD,
            entity_urn=dashboard_urn,
            aspect_name=Constant.BROWSERPATH,
            aspect=browse_path,
        )

        list_of_mcps = [
            browse_path_mcp,
            info_mcp,
            removed_status_mcp,
            dashboard_key_mcp,
        ]

        if owner_mcp is not None:
            list_of_mcps.append(owner_mcp)

        self.append_container_mcp(
            list_of_mcps,
            workspace,
            dashboard_urn,
        )

        self.append_tag_mcp(
            list_of_mcps,
            dashboard_urn,
            Constant.DASHBOARD,
            dashboard.tags,
        )

        return list_of_mcps

    def append_container_mcp(
        self,
        list_of_mcps: List[MetadataChangeProposalWrapper],
        workspace: powerbi_data_classes.Workspace,
        entity_urn: str,
    ) -> None:
        if self.__config.extract_workspaces_to_containers:
            container_key = workspace.get_workspace_key(self.__config.platform_name)
            container_urn = builder.make_container_urn(
                guid=container_key.guid(),
            )

            mcp = MetadataChangeProposalWrapper(
                changeType=ChangeTypeClass.UPSERT,
                entityUrn=entity_urn,
                aspect=ContainerClass(container=f"{container_urn}"),
            )
            list_of_mcps.append(mcp)

    def generate_container_for_workspace(
        self, workspace: powerbi_data_classes.Workspace
    ) -> Iterable[MetadataWorkUnit]:
        workspace_key = workspace.get_workspace_key(self.__config.platform_name)
        container_work_units = gen_containers(
            container_key=workspace_key,
            name=workspace.name,
            sub_types=[BIContainerSubTypes.POWERBI_WORKSPACE],
        )
        return container_work_units

    def append_tag_mcp(
        self,
        list_of_mcps: List[MetadataChangeProposalWrapper],
        entity_urn: str,
        entity_type: str,
        tags: List[str],
    ) -> None:
        if self.__config.extract_endorsements_to_tags and tags:
            tags_mcp = self.new_mcp(
                entity_type=entity_type,
                entity_urn=entity_urn,
                aspect_name=Constant.GLOBAL_TAGS,
                aspect=self.transform_tags(tags),
            )
            list_of_mcps.append(tags_mcp)

    def to_datahub_user(
        self, user: powerbi_data_classes.User
    ) -> List[MetadataChangeProposalWrapper]:
        """
        Map PowerBi user to datahub user
        """

        logger.debug(f"Mapping user {user.displayName}(id={user.id}) to datahub's user")

        # Create an URN for user
        user_urn = builder.make_user_urn(user.get_urn_part())

        user_key = CorpUserKeyClass(username=user.id)

        user_key_mcp = self.new_mcp(
            entity_type=Constant.CORP_USER,
            entity_urn=user_urn,
            aspect_name=Constant.CORP_USER_KEY,
            aspect=user_key,
        )

        return [user_key_mcp]

    def to_datahub_users(
        self, users: List[powerbi_data_classes.User]
    ) -> List[MetadataChangeProposalWrapper]:
        user_mcps = []

        for user in users:
            user_mcps.extend(self.to_datahub_user(user))

        return user_mcps

    def to_datahub_chart(
        self,
        tiles: List[powerbi_data_classes.Tile],
        workspace: powerbi_data_classes.Workspace,
    ) -> Tuple[
        List[MetadataChangeProposalWrapper], List[MetadataChangeProposalWrapper]
    ]:
        ds_mcps = []
        chart_mcps = []

        # Return empty list if input list is empty
        if not tiles:
            return [], []

        logger.info(f"Converting tiles(count={len(tiles)}) to charts")

        for tile in tiles:
            if tile is None:
                continue
            # First convert the dataset to MCP, because dataset mcp is used in input attribute of chart mcp
            dataset_mcps = self.to_datahub_dataset(tile.dataset, workspace)
            # Now convert tile to chart MCP
            chart_mcp = self.to_datahub_chart_mcp(tile, dataset_mcps, workspace)

            ds_mcps.extend(dataset_mcps)
            chart_mcps.extend(chart_mcp)

        # Return dataset and chart MCPs

        return ds_mcps, chart_mcps

    def to_datahub_work_units(
        self,
        dashboard: powerbi_data_classes.Dashboard,
        workspace: powerbi_data_classes.Workspace,
    ) -> List[EquableMetadataWorkUnit]:
        mcps = []

        logger.info(
            f"Converting dashboard={dashboard.displayName} to datahub dashboard"
        )

        # Convert user to CorpUser
        user_mcps: List[MetadataChangeProposalWrapper] = self.to_datahub_users(
            dashboard.users
        )
        # Convert tiles to charts
        ds_mcps, chart_mcps = self.to_datahub_chart(dashboard.tiles, workspace)
        # Lets convert dashboard to datahub dashboard
        dashboard_mcps: List[
            MetadataChangeProposalWrapper
        ] = self.to_datahub_dashboard_mcp(dashboard, workspace, chart_mcps, user_mcps)

        # Now add MCPs in sequence
        mcps.extend(ds_mcps)
        mcps.extend(user_mcps)
        mcps.extend(chart_mcps)
        mcps.extend(dashboard_mcps)

        # Convert MCP to work_units
        work_units = map(self._to_work_unit, mcps)
        # Return set of work_unit
        return deduplicate_list([wu for wu in work_units if wu is not None])

    def pages_to_chart(
        self,
        pages: List[powerbi_data_classes.Page],
        workspace: powerbi_data_classes.Workspace,
        ds_mcps: List[MetadataChangeProposalWrapper],
    ) -> List[MetadataChangeProposalWrapper]:

        chart_mcps = []

        # Return empty list if input list is empty
        if not pages:
            return []

        logger.debug(f"Converting pages(count={len(pages)}) to charts")

        def to_chart_mcps(
            page: powerbi_data_classes.Page,
            ds_mcps: List[MetadataChangeProposalWrapper],
        ) -> List[MetadataChangeProposalWrapper]:
            logger.debug(f"Converting page {page.displayName} to chart")
            # Create a URN for chart
            chart_urn = builder.make_chart_urn(
                self.__config.platform_name, page.get_urn_part()
            )

            logger.debug(f"{Constant.CHART_URN}={chart_urn}")

            ds_input: List[str] = self.to_urn_set(ds_mcps)

            # Create chartInfo mcp
            # Set chartUrl only if tile is created from Report
            chart_info_instance = ChartInfoClass(
                title=page.displayName or "",
                description=page.displayName or "",
                lastModified=ChangeAuditStamps(),
                inputs=ds_input,
                customProperties={Constant.ORDER: str(page.order)},
            )

            info_mcp = self.new_mcp(
                entity_type=Constant.CHART,
                entity_urn=chart_urn,
                aspect_name=Constant.CHART_INFO,
                aspect=chart_info_instance,
            )

            # removed status mcp
            status_mcp = self.new_mcp(
                entity_type=Constant.CHART,
                entity_urn=chart_urn,
                aspect_name=Constant.STATUS,
                aspect=StatusClass(removed=False),
            )
            browse_path = BrowsePathsClass(paths=["/powerbi/{}".format(workspace.name)])
            browse_path_mcp = self.new_mcp(
                entity_type=Constant.CHART,
                entity_urn=chart_urn,
                aspect_name=Constant.BROWSERPATH,
                aspect=browse_path,
            )
            list_of_mcps = [info_mcp, status_mcp, browse_path_mcp]

            self.append_container_mcp(
                list_of_mcps,
                workspace,
                chart_urn,
            )

            return list_of_mcps

        for page in pages:
            if page is None:
                continue
            # Now convert tile to chart MCP
            chart_mcp = to_chart_mcps(page, ds_mcps)
            chart_mcps.extend(chart_mcp)

        return chart_mcps

    def report_to_dashboard(
        self,
        workspace: powerbi_data_classes.Workspace,
        report: powerbi_data_classes.Report,
        chart_mcps: List[MetadataChangeProposalWrapper],
        user_mcps: List[MetadataChangeProposalWrapper],
    ) -> List[MetadataChangeProposalWrapper]:
        """
        Map PowerBi report to Datahub dashboard
        """

        dashboard_urn = builder.make_dashboard_urn(
            self.__config.platform_name, report.get_urn_part()
        )

        chart_urn_list: List[str] = self.to_urn_set(chart_mcps)
        user_urn_list: List[str] = self.to_urn_set(user_mcps)

        # DashboardInfo mcp
        dashboard_info_cls = DashboardInfoClass(
            description=report.description,
            title=report.name or "",
            charts=chart_urn_list,
            lastModified=ChangeAuditStamps(),
            dashboardUrl=report.webUrl,
        )

        info_mcp = self.new_mcp(
            entity_type=Constant.DASHBOARD,
            entity_urn=dashboard_urn,
            aspect_name=Constant.DASHBOARD_INFO,
            aspect=dashboard_info_cls,
        )

        # removed status mcp
        removed_status_mcp = self.new_mcp(
            entity_type=Constant.DASHBOARD,
            entity_urn=dashboard_urn,
            aspect_name=Constant.STATUS,
            aspect=StatusClass(removed=False),
        )

        # dashboardKey mcp
        dashboard_key_cls = DashboardKeyClass(
            dashboardTool=self.__config.platform_name,
            dashboardId=Constant.DASHBOARD_ID.format(report.id),
        )

        # Dashboard key
        dashboard_key_mcp = self.new_mcp(
            entity_type=Constant.DASHBOARD,
            entity_urn=dashboard_urn,
            aspect_name=Constant.DASHBOARD_KEY,
            aspect=dashboard_key_cls,
        )
        # Report Ownership
        owners = [
            OwnerClass(owner=user_urn, type=OwnershipTypeClass.NONE)
            for user_urn in user_urn_list
            if user_urn is not None
        ]

        owner_mcp = None
        if len(owners) > 0:
            # Report owner MCP
            ownership = OwnershipClass(owners=owners)
            owner_mcp = self.new_mcp(
                entity_type=Constant.DASHBOARD,
                entity_urn=dashboard_urn,
                aspect_name=Constant.OWNERSHIP,
                aspect=ownership,
            )

        # Report browsePaths
        browse_path = BrowsePathsClass(
            paths=[f"/{Constant.PLATFORM_NAME}/{workspace.name}"]
        )
        browse_path_mcp = self.new_mcp(
            entity_type=Constant.DASHBOARD,
            entity_urn=dashboard_urn,
            aspect_name=Constant.BROWSERPATH,
            aspect=browse_path,
        )

        sub_type_mcp = self.new_mcp(
            entity_type=Constant.DASHBOARD,
            entity_urn=dashboard_urn,
            aspect_name=SubTypesClass.ASPECT_NAME,
            aspect=SubTypesClass(typeNames=[Constant.REPORT_TYPE_NAME]),
        )

        list_of_mcps = [
            browse_path_mcp,
            info_mcp,
            removed_status_mcp,
            dashboard_key_mcp,
            sub_type_mcp,
        ]

        if owner_mcp is not None:
            list_of_mcps.append(owner_mcp)

        self.append_container_mcp(
            list_of_mcps,
            workspace,
            dashboard_urn,
        )

        self.append_tag_mcp(
            list_of_mcps,
            dashboard_urn,
            Constant.DASHBOARD,
            report.tags,
        )

        return list_of_mcps

    def report_to_datahub_work_units(
        self,
        report: powerbi_data_classes.Report,
        workspace: powerbi_data_classes.Workspace,
    ) -> Iterable[MetadataWorkUnit]:
        mcps: List[MetadataChangeProposalWrapper] = []

        logger.debug(f"Converting dashboard={report.name} to datahub dashboard")

        # Convert user to CorpUser
        user_mcps = self.to_datahub_users(report.users)
        # Convert pages to charts. A report has single dataset and same dataset used in pages to create visualization
        ds_mcps = self.to_datahub_dataset(report.dataset, workspace)
        chart_mcps = self.pages_to_chart(report.pages, workspace, ds_mcps)

        # Let's convert report to datahub dashboard
        report_mcps = self.report_to_dashboard(workspace, report, chart_mcps, user_mcps)

        # Now add MCPs in sequence
        mcps.extend(ds_mcps)
        mcps.extend(user_mcps)
        mcps.extend(chart_mcps)
        mcps.extend(report_mcps)

        # Convert MCP to work_units
        work_units = map(self._to_work_unit, mcps)
        return work_units


@platform_name("PowerBI")
@config_class(PowerBiDashboardSourceConfig)
@support_status(SupportStatus.CERTIFIED)
@capability(
    SourceCapability.OWNERSHIP, "On by default but can disabled by configuration"
)
class PowerBiDashboardSource(StatefulIngestionSourceBase):
    """
    This plugin extracts the following:
    - Power BI dashboards, tiles and datasets
    - Names, descriptions and URLs of dashboard and tile
    - Owners of dashboards
    """

    source_config: PowerBiDashboardSourceConfig
    reporter: PowerBiDashboardSourceReport
    accessed_dashboards: int = 0
    platform: str = "powerbi"

    def __init__(self, config: PowerBiDashboardSourceConfig, ctx: PipelineContext):
        super(PowerBiDashboardSource, self).__init__(config, ctx)
        self.source_config = config
        self.reporter = PowerBiDashboardSourceReport()
        try:
            self.powerbi_client = PowerBiAPI(self.source_config)
        except Exception as e:
            logger.warning(e)
            exit(
                1
            )  # Exit pipeline as we are not able to connect to PowerBI API Service. This exit will avoid raising unwanted stacktrace on console

        self.mapper = Mapper(config, self.reporter)

        # Create and register the stateful ingestion use-case handler.
        self.stale_entity_removal_handler = StaleEntityRemovalHandler(
            source=self,
            config=self.source_config,
            state_type_class=BaseSQLAlchemyCheckpointState,
            pipeline_name=ctx.pipeline_name,
            run_id=ctx.run_id,
        )

    def get_platform_instance_id(self) -> Optional[str]:
        return self.source_config.platform_name

    @classmethod
    def create(cls, config_dict, ctx):
        config = PowerBiDashboardSourceConfig.parse_obj(config_dict)
        return cls(config, ctx)

    def get_allowed_workspaces(self) -> Iterable[powerbi_data_classes.Workspace]:
        all_workspaces = self.powerbi_client.get_workspaces()
        allowed_wrk = [
            workspace
            for workspace in all_workspaces
            if self.source_config.workspace_id_pattern.allowed(workspace.id)
        ]

        logger.info(f"Number of workspaces = {len(all_workspaces)}")
        self.reporter.report_number_of_workspaces(len(all_workspaces))
        logger.info(f"Number of allowed workspaces = {len(allowed_wrk)}")
        logger.debug(f"Workspaces = {all_workspaces}")

        return allowed_wrk

    def validate_dataset_type_mapping(self):
        powerbi_data_platforms: List[str] = [
            data_platform.value.powerbi_data_platform_name
            for data_platform in resolver.SupportedDataPlatform
        ]

        for key in self.source_config.dataset_type_mapping.keys():
            if key not in powerbi_data_platforms:
                raise ValueError(f"PowerBI DataPlatform {key} is not supported")

    def get_workunits_internal(self) -> Iterable[MetadataWorkUnit]:
        """
        Datahub Ingestion framework invoke this method
        """
        logger.info("PowerBi plugin execution is started")
        # Validate dataset type mapping
        self.validate_dataset_type_mapping()
        # Fetch PowerBi workspace for given workspace identifier
        for workspace in self.get_allowed_workspaces():
            logger.info(f"Scanning workspace id: {workspace.id}")
            self.powerbi_client.fill_workspace(workspace, self.reporter)

            if self.source_config.extract_workspaces_to_containers:
                workspace_workunits = self.mapper.generate_container_for_workspace(
                    workspace
                )

                for workunit in workspace_workunits:
                    # Return workunit to Datahub Ingestion framework
                    yield workunit
            for dashboard in workspace.dashboards:
                try:
                    # Fetch PowerBi users for dashboards
                    dashboard.users = self.powerbi_client.get_dashboard_users(dashboard)
                    # Increase dashboard and tiles count in report
                    self.reporter.report_dashboards_scanned()
                    self.reporter.report_charts_scanned(count=len(dashboard.tiles))
                except Exception as e:
                    message = f"Error ({e}) occurred while loading dashboard {dashboard.displayName}(id={dashboard.id}) tiles."

                    logger.exception(message, e)
                    self.reporter.report_warning(dashboard.id, message)
                # Convert PowerBi Dashboard and child entities to Datahub work unit to ingest into Datahub
                workunits = self.mapper.to_datahub_work_units(dashboard, workspace)
                for workunit in workunits:
                    # Return workunit to Datahub Ingestion framework
                    yield workunit

            for report in workspace.reports:
                for work_unit in self.mapper.report_to_datahub_work_units(
                    report, workspace
                ):
                    yield work_unit

    def get_workunits(self) -> Iterable[MetadataWorkUnit]:
        return auto_stale_entity_removal(
            self.stale_entity_removal_handler,
            auto_workunit_reporter(
                self.reporter, auto_status_aspect(self.get_workunits_internal())
            ),
        )

    def get_report(self) -> SourceReport:
        return self.reporter
