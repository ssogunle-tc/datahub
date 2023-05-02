package datahub.client.patch.common;

import com.fasterxml.jackson.databind.node.ObjectNode;
import com.linkedin.common.TagUrn;
import datahub.client.patch.AbstractMultiFieldPatchBuilder;
import datahub.client.patch.PatchOperationType;
import javax.annotation.Nonnull;
import javax.annotation.Nullable;
import org.apache.commons.lang3.tuple.ImmutableTriple;

import static com.fasterxml.jackson.databind.node.JsonNodeFactory.*;
import static com.linkedin.metadata.Constants.*;


public class GlobalTagsPatchBuilder extends AbstractMultiFieldPatchBuilder<GlobalTagsPatchBuilder> {

  private static final String BASE_PATH = "/tags/";
  private static final String URN_KEY = "urn";
  private static final String CONTEXT_KEY = "context";

  /**
   * Adds a tag with an optional context string
   * @param urn required
   * @param context optional
   * @return
   */
  public GlobalTagsPatchBuilder addTag(@Nonnull TagUrn urn, @Nullable String context) {
    ObjectNode value = instance.objectNode();
    value.put(URN_KEY, urn.toString());

    if (context != null) {
      value.put(CONTEXT_KEY, context);
    }

    pathValues.add(ImmutableTriple.of(PatchOperationType.ADD.getValue(), BASE_PATH + urn, value));
    return this;
  }

  public GlobalTagsPatchBuilder removeTag(@Nonnull TagUrn urn) {
    pathValues.add(ImmutableTriple.of(PatchOperationType.REMOVE.getValue(), BASE_PATH + urn, null));
    return this;
  }

  @Override
  protected String getAspectName() {
    return GLOBAL_TAGS_ASPECT_NAME;
  }

  @Override
  protected String getEntityType() {
    if (this.targetEntityUrn == null) {
      throw new IllegalStateException("Target Entity Urn must be set to determine entity type before building Patch.");
    }
    return this.targetEntityUrn.getEntityType();
  }
}
