package com.linkedin.metadata.kafka;

import com.linkedin.gms.factory.entity.RestliEntityClientFactory;
import com.linkedin.gms.factory.spring.YamlPropertySourceFactory;
import com.linkedin.gms.factory.telemetry.ScheduledAnalyticsFactory;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.actuate.autoconfigure.solr.SolrHealthContributorAutoConfiguration;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.autoconfigure.cassandra.CassandraAutoConfiguration;
import org.springframework.boot.autoconfigure.elasticsearch.ElasticsearchRestClientAutoConfiguration;
import org.springframework.context.annotation.ComponentScan;
import org.springframework.context.annotation.FilterType;
import org.springframework.context.annotation.PropertySource;


@SuppressWarnings("checkstyle:HideUtilityClassConstructor")
@SpringBootApplication(exclude = {
        ElasticsearchRestClientAutoConfiguration.class,
        CassandraAutoConfiguration.class,
        SolrHealthContributorAutoConfiguration.class
})
@ComponentScan(basePackages = {
        "com.linkedin.metadata.boot.kafka",
        "com.linkedin.gms.factory.auth",
        "com.linkedin.gms.factory.common",
        "com.linkedin.gms.factory.config",
        "com.linkedin.gms.factory.entity",
        "com.linkedin.gms.factory.entityregistry",
        "com.linkedin.gms.factory.kafka",
        "com.linkedin.gms.factory.search",
        "com.linkedin.gms.factory.secret",
        "com.linkedin.gms.factory.timeseries",
        "com.linkedin.restli.server",
        "com.linkedin.metadata.restli",
        "com.linkedin.metadata.kafka",
        "com.linkedin.metadata.dao.producer"
}, excludeFilters = {
        @ComponentScan.Filter(type = FilterType.ASSIGNABLE_TYPE, classes = {
                ScheduledAnalyticsFactory.class,
                RestliEntityClientFactory.class
        })
})
@PropertySource(value = "classpath:/application.yml", factory = YamlPropertySourceFactory.class)
public class MceConsumerApplication {

  public static void main(String[] args) {
    SpringApplication.run(MceConsumerApplication.class, args);
  }
}
