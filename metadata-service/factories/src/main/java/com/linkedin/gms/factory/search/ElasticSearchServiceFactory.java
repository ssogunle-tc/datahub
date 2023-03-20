package com.linkedin.gms.factory.search;

import com.linkedin.gms.factory.config.ConfigurationProvider;
import com.linkedin.gms.factory.entityregistry.EntityRegistryFactory;
import com.linkedin.gms.factory.spring.YamlPropertySourceFactory;
import com.linkedin.metadata.models.registry.EntityRegistry;
import com.linkedin.metadata.search.elasticsearch.ElasticSearchService;
import com.linkedin.metadata.search.elasticsearch.indexbuilder.EntityIndexBuilders;
import com.linkedin.metadata.search.elasticsearch.indexbuilder.SettingsBuilder;
import com.linkedin.metadata.search.elasticsearch.query.ESBrowseDAO;
import com.linkedin.metadata.search.elasticsearch.query.ESSearchDAO;
import com.linkedin.metadata.search.elasticsearch.update.ESWriteDAO;
import javax.annotation.Nonnull;

import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Import;
import org.springframework.context.annotation.PropertySource;


@Slf4j
@Configuration
@PropertySource(value = "classpath:/application.yml", factory = YamlPropertySourceFactory.class)
@Import({EntityRegistryFactory.class, SettingsBuilderFactory.class, ConfigurationProvider.class})
public class ElasticSearchServiceFactory {
  @Autowired
  @Qualifier("baseElasticSearchComponents")
  private BaseElasticSearchComponentsFactory.BaseElasticSearchComponents components;

  @Autowired
  @Qualifier("entityRegistry")
  private EntityRegistry entityRegistry;

  @Autowired
  @Qualifier("settingsBuilder")
  private SettingsBuilder settingsBuilder;

  @Autowired
  private ConfigurationProvider configurationProvider;

  @Bean(name = "elasticSearchService")
  @Nonnull
  protected ElasticSearchService getInstance(ConfigurationProvider configurationProvider) {
    log.info("Search configuration: {}", configurationProvider.getElasticSearch().getSearch());

    ESSearchDAO esSearchDAO =
        new ESSearchDAO(entityRegistry, components.getSearchClient(), components.getIndexConvention(),
            configurationProvider.getFeatureFlags().isPointInTimeCreationEnabled(),
            configurationProvider.getElasticSearch().getImplementation(), configurationProvider.getElasticSearch().getSearch());
    return new ElasticSearchService(
        new EntityIndexBuilders(components.getIndexBuilder(), entityRegistry, components.getIndexConvention(),
            settingsBuilder), esSearchDAO,
        new ESBrowseDAO(entityRegistry, components.getSearchClient(), components.getIndexConvention()),
        new ESWriteDAO(entityRegistry, components.getSearchClient(), components.getIndexConvention(),
            components.getBulkProcessor(), components.getNumRetries()));
  }
}
