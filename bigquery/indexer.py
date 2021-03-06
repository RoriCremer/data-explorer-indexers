"""Indexes BigQuery tables."""
import argparse
import json
import logging
import os
import pandas as pd
import time
from elasticsearch_dsl import Search
from google.cloud import bigquery
from google.cloud import exceptions
from google.cloud import storage

from indexer_util import indexer_util

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(filename)10s:%(lineno)s %(levelname)s %(message)s',
    datefmt='%Y%m%d%H:%M:%S')
logger = logging.getLogger('indexer.bigquery')

UPDATE_SAMPLES_SCRIPT = """
if (!ctx._source.containsKey('samples')) {
   ctx._source.samples = [params.sample]
} else {
   // If this sample already exists, merge it with the new one.
   int removeIdx = -1;
   for (int i = 0; i < ctx._source.samples.size(); i++) {
      if (ctx._source.samples.get(i).get('%s').equals(params.sample.get('%s'))) {
         removeIdx = i;
      }
   }

   if (removeIdx >= 0) {
      Map merged = ctx._source.samples.remove(removeIdx);
      merged.putAll(params.sample);
      ctx._source.samples.add(merged);
   } else {
      ctx._source.samples.add(params.sample);
   }
}
"""


# Copied from https://stackoverflow.com/a/45392259
def _environ_or_required(key):
    if os.environ.get(key):
        return {'default': os.environ.get(key)}
    else:
        return {'required': True}


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--elasticsearch_url',
        type=str,
        help='Elasticsearch url. Must start with http://',
        default=os.environ.get('ELASTICSEARCH_URL'))
    parser.add_argument(
        '--dataset_config_dir',
        type=str,
        help='Directory containing config files. Can be relative or absolute.',
        default=os.environ.get('DATASET_CONFIG_DIR'))
    parser.add_argument(
        '--billing_project_id',
        type=str,
        help=
        'The project that will be billed for querying BigQuery tables. The account running this script must have bigquery.jobs.create permission on this project.',
        **_environ_or_required('BILLING_PROJECT_ID'))
    return parser.parse_args()


def _get_nested_mappings(schema, prefix=None):
    # Find all repeated record type fields and create mappings for them
    # recursively.
    nested = {}
    for field in schema:
        name = '%s.%s' % (prefix, field.name) if prefix else field.name
        inner_nested = _get_nested_mappings(field.fields)
        if field.mode == 'REPEATED' and field.field_type == 'RECORD':
            nested[name] = {}
            nested[name]['type'] = "nested"
        if inner_nested:
            if name not in nested:
                nested[name] = {}
            nested[name]['properties'] = inner_nested
    return nested if nested else None


def _table_name_from_table(table):
    # table.full_table_id is the legacy format: project id:dataset id.table name
    # Convert to Standard SQL format: project id.dataset id.table name
    # Use rsplit instead of split because project id may have ":", eg
    # "google.com:api-project-123".
    project_id, dataset_table_id = table.full_table_id.rsplit(':', 1)
    return project_id + '.' + dataset_table_id


def _create_nested_mappings(es, index_name, table, sample_id_column):
    # Create nested mappings for repeated record type BigQuery fields so that
    # queries will work correctly, see:
    # https://www.elastic.co/guide/en/elasticsearch/reference/6.4/nested.html#_how_arrays_of_objects_are_flattened
    nested = _get_nested_mappings(table.schema, _table_name_from_table(table))
    # If the table contains the sample ID column, add a nested samples mapping.
    if sample_id_column in [f.name for f in table.schema]:
        logger.info('Adding nested sample mapping to %s.' % index_name)
        sample_mapping = {'properties': {'samples': {'type': 'nested'}}}
        if nested:
            sample_mapping['properties']['samples']['properties'] = nested
        es.indices.put_mapping(
            doc_type='type', index=index_name, body=sample_mapping)
    elif nested:
        logger.info('Adding neseted mappings to %s.' % index_name)
        es.indices.put_mapping(
            doc_type='type', index=index_name, body={'properties': nested})


def _docs_by_id(df, table_name, participant_id_column):
    for _, row in df.iterrows():
        # Remove nan's as described in
        # https://stackoverflow.com/questions/40363926/how-do-i-convert-my-dataframe-into-a-dictionary-while-ignoring-the-nan-values
        # Elasticsearch crashes when indexing nan's.
        row_dict = row.dropna().to_dict()
        # Remove the participant_id_column since it is stored as document id.
        del row_dict[participant_id_column]
        row_dict = {table_name + '.' + k: v for k, v in row_dict.iteritems()}
        yield row[participant_id_column], row_dict


def _field_docs_by_id(id_prefix, name_prefix, fields):
    # This method is recursive to handle nested fields (BigQuery RECORD columns).
    # For nested fields, field name includes all levels of nesting, eg "addresses.city".
    for field in fields:
        field_name = field.name
        field_id = field.name
        if name_prefix:
            field_name = name_prefix + '.' + field_name
        if id_prefix:
            field_id = id_prefix + '.' + field_id
        # For 'RECORD' fields, we want to index only the sub fields. For example
        # if 'address' has {city, state, zip}, we want to index 'address.city',
        # 'address.state' and 'address.zip'.
        if field.field_type == 'RECORD':
            for field_doc in _field_docs_by_id(field_id, field_name,
                                               field.fields):
                yield field_doc
        else:
            field_dict = {'name': field_name}
            if field.description:
                field_dict['description'] = field.description
            yield field_id, field_dict


# Sample and participant tables need to be indexed differently.
# For participant tables, we can use partial updates
# (https://www.elastic.co/guide/en/elasticsearch/reference/current/docs-update.html#_updates_with_a_partial_document)
#
# If one participant table has weight and another has height:
# - First weight table is indexed. Participant documents get a weight field.
# - Then height table is indexed. Participant documents get a height field. The
#   weight fields are unchanged.
#
# Now say one sample table has center and another has platform.
# - First center table is indexed. For each participant document, a samples
#   field is created. The samples field contains an array of nested objects,
#   each of which has a center field.
# - Then platform table is indexed. For each participant document, the samples
#   field is overwritten to the new value, which contains platform and not
#   center.
# In order to keep the center field, one must use a script. See
# https://discuss.elastic.co/t/updating-nested-objects/87586/2 and
# https://www.elastic.co/guide/en/elasticsearch/reference/6.4/docs-update.html
def _sample_scripts_by_id(df, table_name, participant_id_column,
                          sample_id_column, sample_file_columns):
    for _, row in df.iterrows():
        # Remove nan's as described in
        # https://stackoverflow.com/questions/40363926/how-do-i-convert-my-dataframe-into-a-dictionary-while-ignoring-the-nan-values
        # Elasticsearch crashes when indexing nan's.
        row_dict = row.dropna().to_dict()
        # Remove the participant_id_column since it is stored as document id.
        del row_dict[participant_id_column]
        # Use the sample_id_column without the project_id + dataset qualification.
        row_dict = {
            table_name + '.' + k if k != sample_id_column else k: v
            for k, v in row_dict.iteritems()
        }

        # Use the sample_file_columns configuration to add the internal
        # '_has_<sample_file_type>' fields to the samples index.
        for file_type, col in sample_file_columns.iteritems():
            # Only mark as false if this sample file column is relevant to the
            # table currently being indexed.
            if table_name in col:
                has_name = '_has_%s' % file_type.lower().replace(" ", "_")
                if col in row_dict and row_dict[col]:
                    row_dict[has_name] = True
                else:
                    row_dict[has_name] = False

        script = UPDATE_SAMPLES_SCRIPT % (sample_id_column, sample_id_column)
        yield row[participant_id_column], {
            'source': script,
            'lang': 'painless',
            'params': {
                'sample': row_dict
            }
        }


def index_table(es, index_name, client, table, participant_id_column,
                sample_id_column, sample_file_columns, billing_project_id):
    """Indexes a BigQuery table.

    Args:
        es: Elasticsearch object.
        index_name: Name of Elasticsearch index.
        table_name: Fully-qualified table name of the format:
            "<project id>.<dataset id>.<table name>"
        participant_id_column: Name of the column containing the participant ID.
        sample_id_column: (optional) Name of the column containing the sample ID
            (only needed on samples tables).
        sample_file_columns: (optional) Mappings for columns which contain genomic
            files of a particular type (specified in ui.json).
        billing_project_id: GCP project ID to bill for reading table
    """
    _create_nested_mappings(es, index_name, table, sample_id_column)
    table_name = _table_name_from_table(table)
    start_time = time.time()
    logger.info('Indexing %s into %s.' % (table_name, index_name))

    # There is no easy way to import BigQuery -> Elasticsearch. Instead:
    # BigQuery table -> pandas dataframe -> dict -> Elasticsearch
    df = pd.read_gbq(
        'SELECT * FROM `%s`' % table_name,
        project_id=billing_project_id,
        dialect='standard')
    elapsed_time = time.time() - start_time
    elapsed_time_str = time.strftime('%Hh:%Mm:%Ss', time.gmtime(elapsed_time))
    logger.info('BigQuery -> pandas took %s' % elapsed_time_str)
    logger.info('%s has %d rows' % (table_name, len(df)))

    if not participant_id_column in df.columns:
        raise ValueError(
            'Participant ID column %s not found in BigQuery table %s' %
            (participant_id_column, table_name))

    if sample_id_column in df.columns:
        scripts_by_id = _sample_scripts_by_id(
            df, table_name, participant_id_column, sample_id_column,
            sample_file_columns)
        indexer_util.bulk_index_scripts(es, index_name, scripts_by_id)
    else:
        docs_by_id = _docs_by_id(df, table_name, participant_id_column)
        indexer_util.bulk_index_docs(es, index_name, docs_by_id)

    elapsed_time = time.time() - start_time
    elapsed_time_str = time.strftime("%Hh:%Mm:%Ss", time.gmtime(elapsed_time))
    logger.info('pandas -> ElasticSearch index took %s' % elapsed_time_str)


def index_fields(es, index_name, table, sample_id_column):
    table_name = _table_name_from_table(table)
    logger.info('Indexing %s into %s.' % (table_name, index_name))

    id_prefix = table_name
    fields = table.schema
    # If the table contains the sample_id_columnm, prefix the elasticsearch Name
    # of the fields in this table with "samples."
    # This is needed to differentiate the sample facets for special handling.
    for field in fields:
        if field.name == sample_id_column:
            id_prefix = "samples." + id_prefix

    field_docs = _field_docs_by_id(id_prefix, '', fields)
    indexer_util.bulk_index_docs(es, index_name, field_docs)


def read_table(client, table_name):
    # Use rsplit instead of split because project id may have ".", eg
    # "google.com:api-project-123".
    project_id, dataset_id, table_name = table_name.rsplit('.', 2)
    return client.get_table(
        client.dataset(dataset_id, project=project_id).table(table_name))


def create_samples_json_export_file(es, index_name, deploy_project_id):
    """
    Writes the samples export JSON file to a GCS bucket. This significantly
    speeds up exporting the samples table to Terra in the Data Explorer.

    Args:
        es: Elasticsearch object.
        index_name: Name of Elasticsearch index.
        deploy_project_id: Google Cloud Project ID containing the export samples bucket
    """
    entities = []
    search = Search(using=es, index=index_name)
    for hit in search.scan():
        participant_id = hit.meta['id']
        doc = hit.to_dict()
        for sample in doc.get('samples', []):
            sample_id = sample['sample_id']
            export_sample = {'participant': participant_id}
            for es_field_name, value in sample.iteritems():
                # es_field_name looks like "_has_chr_18_vcf", "sample_id" or
                # "verily-public-data.human_genome_variants.1000_genomes_sample_info.In_Low_Coverage_Pilot".
                splits = es_field_name.split('.')
                # Ignore _has_* and sample_id fields.
                if len(splits) != 4:
                    continue
                export_sample[splits[3]] = value

            entities.append({
                'entityType': 'sample',
                'name': sample_id,
                'attributes': export_sample,
            })

    client = storage.Client(project=deploy_project_id)
    # Don't put in project_id-export because that bucket has TTL= 1 day.
    bucket_name = '%s-export-samples' % deploy_project_id
    bucket = client.lookup_bucket(bucket_name)
    if not bucket:
        bucket = client.create_bucket(bucket_name)
    blob = bucket.blob('samples')
    entities_json = json.dumps(entities, indent=4)
    # Remove the trailing ']' character to allow this JSON to be merged
    # with JSON for additional entities using the GCS compose API:
    # https://cloud.google.com/storage/docs/json_api/v1/objects/compose
    entities_json = entities_json[:-1]
    blob.upload_from_string(entities_json)
    logger.info('Wrote gs://%s/samples' % (bucket_name))


def main():
    args = _parse_args()
    # Read dataset config files
    index_name = indexer_util.get_index_name(args.dataset_config_dir)
    bigquery_config_path = os.path.join(args.dataset_config_dir,
                                        'bigquery.json')
    bigquery_config = indexer_util.parse_json_file(bigquery_config_path)
    deploy_config_path = os.path.join(args.dataset_config_dir, 'deploy.json')
    es = indexer_util.maybe_create_elasticsearch_index(args.elasticsearch_url,
                                                       index_name)

    participant_id_column = bigquery_config['participant_id_column']
    sample_id_column = bigquery_config.get('sample_id_column', None)
    sample_file_columns = bigquery_config.get('sample_file_columns', {})
    client = bigquery.Client(project=args.billing_project_id)

    for table_name in bigquery_config['table_names']:
        table = read_table(client, table_name)
        index_table(es, index_name, client, table, participant_id_column,
                    sample_id_column, sample_file_columns,
                    args.billing_project_id)
        index_fields(es, index_name + '_fields', table, sample_id_column)

    # Ensure all of the newly indexed documents are loaded into ES.
    time.sleep(5)
    if os.path.exists(deploy_config_path):
        deploy_config = indexer_util.parse_json_file(deploy_config_path)
        create_samples_json_export_file(es, index_name,
                                        deploy_config['project_id'])


if __name__ == '__main__':
    main()
