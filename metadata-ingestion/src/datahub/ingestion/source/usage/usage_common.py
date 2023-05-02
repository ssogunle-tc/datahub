import dataclasses
import logging
from collections import defaultdict
from datetime import datetime
from typing import (
    Callable,
    Counter,
    Dict,
    Generic,
    Iterable,
    List,
    Optional,
    Tuple,
    TypeVar,
)

import pydantic
from pydantic.fields import Field

import datahub.emitter.mce_builder as builder
from datahub.configuration.common import AllowDenyPattern
from datahub.configuration.time_window_config import (
    BaseTimeWindowConfig,
    BucketDuration,
    get_time_bucket,
)
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.api.workunit import MetadataWorkUnit
from datahub.metadata.schema_classes import (
    DatasetFieldUsageCountsClass,
    DatasetUsageStatisticsClass,
    DatasetUserUsageCountsClass,
    TimeWindowSizeClass,
)
from datahub.utilities.sql_formatter import format_sql_query, trim_query

logger = logging.getLogger(__name__)

ResourceType = TypeVar("ResourceType")

# The total number of characters allowed across all queries in a single workunit.
TOTAL_BUDGET_FOR_QUERY_LIST = 24000


def default_user_urn_builder(email: str) -> str:
    return builder.make_user_urn(email.split("@")[0])


def make_usage_workunit(
    bucket_start_time: datetime,
    resource: ResourceType,
    query_count: int,
    query_freq: Optional[List[Tuple[str, int]]],
    user_freq: List[Tuple[str, int]],
    column_freq: List[Tuple[str, int]],
    bucket_duration: BucketDuration,
    resource_urn_builder: Callable[[ResourceType], str],
    top_n_queries: int,
    format_sql_queries: bool,
    user_urn_builder: Optional[Callable[[str], str]] = None,
    total_budget_for_query_list: int = TOTAL_BUDGET_FOR_QUERY_LIST,
    query_trimmer_string: str = " ...",
) -> MetadataWorkUnit:
    if user_urn_builder is None:
        user_urn_builder = default_user_urn_builder

    top_sql_queries: Optional[List[str]] = None
    if query_freq is not None:
        budget_per_query: int = int(total_budget_for_query_list / top_n_queries)
        top_sql_queries = [
            trim_query(
                format_sql_query(query, keyword_case="upper", reindent_aligned=True)
                if format_sql_queries
                else query,
                budget_per_query=budget_per_query,
                query_trimmer_string=query_trimmer_string,
            )
            for query, _ in query_freq
        ]

    usageStats = DatasetUsageStatisticsClass(
        timestampMillis=int(bucket_start_time.timestamp() * 1000),
        eventGranularity=TimeWindowSizeClass(unit=bucket_duration, multiple=1),
        uniqueUserCount=len(user_freq),
        totalSqlQueries=query_count,
        topSqlQueries=top_sql_queries,
        userCounts=[
            DatasetUserUsageCountsClass(
                user=user_urn_builder(user),
                count=count,
                userEmail=user if "@" in user else None,
            )
            for user, count in user_freq
        ],
        fieldCounts=[
            DatasetFieldUsageCountsClass(
                fieldPath=column,
                count=count,
            )
            for column, count in column_freq
        ],
    )

    return MetadataChangeProposalWrapper(
        entityUrn=resource_urn_builder(resource),
        aspect=usageStats,
    ).as_workunit()


@dataclasses.dataclass
class GenericAggregatedDataset(Generic[ResourceType]):
    bucket_start_time: datetime
    resource: ResourceType

    readCount: int = 0
    queryCount: int = 0

    queryFreq: Counter[str] = dataclasses.field(default_factory=Counter)
    userFreq: Counter[str] = dataclasses.field(default_factory=Counter)
    columnFreq: Counter[str] = dataclasses.field(default_factory=Counter)

    def add_read_entry(
        self,
        user_email: str,
        query: Optional[str],
        fields: List[str],
        user_email_pattern: AllowDenyPattern = AllowDenyPattern.allow_all(),
    ) -> None:
        if not user_email_pattern.allowed(user_email):
            return

        self.readCount += 1
        self.userFreq[user_email] += 1

        if query:
            self.queryCount += 1
            self.queryFreq[query] += 1
        for column in fields:
            self.columnFreq[column] += 1

    def make_usage_workunit(
        self,
        bucket_duration: BucketDuration,
        resource_urn_builder: Callable[[ResourceType], str],
        top_n_queries: int,
        format_sql_queries: bool,
        include_top_n_queries: bool,
        user_urn_builder: Optional[Callable[[str], str]] = None,
        total_budget_for_query_list: int = TOTAL_BUDGET_FOR_QUERY_LIST,
        query_trimmer_string: str = " ...",
    ) -> MetadataWorkUnit:
        query_freq = (
            self.queryFreq.most_common(top_n_queries) if include_top_n_queries else None
        )
        return make_usage_workunit(
            bucket_start_time=self.bucket_start_time,
            resource=self.resource,
            query_count=self.queryCount,
            query_freq=query_freq,
            user_freq=self.userFreq.most_common(),
            column_freq=self.columnFreq.most_common(),
            bucket_duration=bucket_duration,
            resource_urn_builder=resource_urn_builder,
            user_urn_builder=user_urn_builder,
            top_n_queries=top_n_queries,
            format_sql_queries=format_sql_queries,
            total_budget_for_query_list=total_budget_for_query_list,
            query_trimmer_string=query_trimmer_string,
        )


class BaseUsageConfig(BaseTimeWindowConfig):
    top_n_queries: pydantic.PositiveInt = Field(
        default=10, description="Number of top queries to save to each table."
    )
    user_email_pattern: AllowDenyPattern = Field(
        default=AllowDenyPattern.allow_all(),
        description="regex patterns for user emails to filter in usage.",
    )
    include_operational_stats: bool = Field(
        default=True, description="Whether to display operational stats."
    )

    include_read_operational_stats: bool = Field(
        default=False,
        description="Whether to report read operational stats. Experimental.",
    )

    format_sql_queries: bool = Field(
        default=False, description="Whether to format sql queries"
    )
    include_top_n_queries: bool = Field(
        default=True, description="Whether to ingest the top_n_queries."
    )

    @pydantic.validator("top_n_queries")
    def ensure_top_n_queries_is_not_too_big(cls, v: int) -> int:
        minimum_query_size = 20

        max_queries = int(TOTAL_BUDGET_FOR_QUERY_LIST / minimum_query_size)
        if v > max_queries:
            raise ValueError(
                f"top_n_queries is set to {v} but it can be maximum {max_queries}"
            )
        return v


class UsageAggregator(Generic[ResourceType]):
    # TODO: Move over other connectors to use this class

    def __init__(self, config: BaseUsageConfig):
        self.config = config
        self.aggregation: Dict[
            datetime, Dict[ResourceType, GenericAggregatedDataset[ResourceType]]
        ] = defaultdict(dict)

    def aggregate_event(
        self,
        *,
        resource: ResourceType,
        start_time: datetime,
        query: Optional[str],
        user: str,
        fields: List[str],
    ) -> None:
        floored_ts: datetime = get_time_bucket(start_time, self.config.bucket_duration)
        self.aggregation[floored_ts].setdefault(
            resource,
            GenericAggregatedDataset[ResourceType](
                bucket_start_time=floored_ts,
                resource=resource,
            ),
        ).add_read_entry(
            user,
            query,
            fields,
        )

    def generate_workunits(
        self,
        resource_urn_builder: Callable[[ResourceType], str],
        user_urn_builder: Optional[Callable[[str], str]] = None,
    ) -> Iterable[MetadataWorkUnit]:
        for time_bucket in self.aggregation.values():
            for aggregate in time_bucket.values():
                yield aggregate.make_usage_workunit(
                    bucket_duration=self.config.bucket_duration,
                    top_n_queries=self.config.top_n_queries,
                    format_sql_queries=self.config.format_sql_queries,
                    include_top_n_queries=self.config.include_top_n_queries,
                    resource_urn_builder=resource_urn_builder,
                    user_urn_builder=user_urn_builder,
                )
