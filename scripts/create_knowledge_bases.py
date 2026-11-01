"""
create_knowledge_bases.py
=========================
Creates the three NovaMart Bedrock Knowledge Bases (Task 5) from the terminal -
the scripted equivalent of the console steps in the project instructions:

  Returns  KB  <- s3://<policy bucket>/policies/returns/   -> returns-policy-index
  Shipping KB  <- s3://<policy bucket>/policies/shipping/  -> shipping-policy-index
  Warranty KB  <- s3://<policy bucket>/policies/warranty/  -> warranty-policy-index

Each one: Titan Embed Text v2 (1024 dims, float32), self-managed S3 Vectors
store using the existing vector bucket + index from the CloudFormation stack.
For each KB the script creates the KB, adds the S3 data source, runs a sync
(ingestion job) and waits for it to complete. The IDs are written to .env.

Safe to re-run: existing KBs / data sources are reused and re-synced.

Usage:
    python scripts/create_knowledge_bases.py
"""

import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import boto3                                  # noqa: E402
from botocore.exceptions import ClientError   # noqa: E402
import config                                 # noqa: E402

REGION   = config.AWS_REGION
ENV_FILE = os.path.join(ROOT, '.env')
EMBEDDING_MODEL_ARN = f"arn:aws:bedrock:{REGION}::foundation-model/amazon.titan-embed-text-v2:0"

# domain -> (KB name, S3 prefix, vector index config attribute, .env key)
KNOWLEDGE_BASES = [
    ('Returns',  'novamart-returns-policy-kb',  'policies/returns/',  'RETURNS_VECTOR_INDEX',  'RETURNS_KB_ID'),
    ('Shipping', 'novamart-shipping-policy-kb', 'policies/shipping/', 'SHIPPING_VECTOR_INDEX', 'SHIPPING_KB_ID'),
    ('Warranty', 'novamart-warranty-policy-kb', 'policies/warranty/', 'WARRANTY_VECTOR_INDEX', 'WARRANTY_KB_ID'),
]

agent = boto3.client('bedrock-agent', region_name=REGION)


def wait_for(fetch, ok_states, fail_states, label, timeout=600):
    """Poll fetch() until its status is in ok_states. Raises on failure/timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, detail = fetch()
        if status in ok_states:
            print(f"    {label}: {status}")
            return detail
        if status in fail_states:
            raise RuntimeError(f"{label} {status}: {detail}")
        time.sleep(5)
    raise TimeoutError(f"{label} not ready after {timeout}s")


def find_kb(name):
    for page in agent.get_paginator('list_knowledge_bases').paginate():
        for kb in page['knowledgeBaseSummaries']:
            if kb['name'] == name:
                return kb['knowledgeBaseId']
    return None


def create_kb(name, domain, index_name):
    """Create one KB on the existing S3 Vectors index (self-managed vector store)."""
    bucket_arn = config.VECTOR_STORE_BUCKET_ARN
    index_arn  = f"{bucket_arn}/index/{index_name}"
    common = dict(
        name=name,
        description=f"NovaMart {domain} policy documents (Task 5)",
        # The stack's AgentCore role trusts bedrock.amazonaws.com and already has
        # bedrock:InvokeModel (Titan), read access to the policy bucket and s3vectors:*.
        roleArn=config.AGENTCORE_ROLE_ARN,
        knowledgeBaseConfiguration={
            'type': 'VECTOR',
            'vectorKnowledgeBaseConfiguration': {
                'embeddingModelArn': EMBEDDING_MODEL_ARN,
                'embeddingModelConfiguration': {
                    'bedrockEmbeddingModelConfiguration': {
                        'dimensions': 1024,                 # must match the index dimension
                        'embeddingDataType': 'FLOAT32',
                    }
                },
            },
        },
    )
    try:
        resp = agent.create_knowledge_base(**common, storageConfiguration={
            'type': 'S3_VECTORS',
            's3VectorsConfiguration': {'indexArn': index_arn},
        })
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ValidationException':
            raise
        # Some API versions expect the bucket ARN + index name instead of the index ARN
        resp = agent.create_knowledge_base(**common, storageConfiguration={
            'type': 'S3_VECTORS',
            's3VectorsConfiguration': {'vectorBucketArn': bucket_arn, 'indexName': index_name},
        })
    return resp['knowledgeBase']['knowledgeBaseId']


def ensure_data_source(kb_id, domain, prefix):
    existing = agent.list_data_sources(knowledgeBaseId=kb_id)['dataSourceSummaries']
    if existing:
        return existing[0]['dataSourceId']
    resp = agent.create_data_source(
        knowledgeBaseId=kb_id,
        name=f"{domain.lower()}-policy-docs",
        description=f"s3://{config.POLICY_BUCKET}/{prefix}",
        dataSourceConfiguration={
            'type': 'S3',
            's3Configuration': {
                'bucketArn': f"arn:aws:s3:::{config.POLICY_BUCKET}",
                'inclusionPrefixes': [prefix],     # each KB only sees its own folder
            },
        },
        # default chunking - the policy documents are short
    )
    return resp['dataSource']['dataSourceId']


def sync(kb_id, ds_id):
    """Start an ingestion job (the console's Sync button) and wait for it."""
    job = agent.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)['ingestionJob']

    def fetch():
        j = agent.get_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id,
                                    ingestionJobId=job['ingestionJobId'])['ingestionJob']
        return j['status'], j.get('statistics') or j.get('failureReasons')
    stats = wait_for(fetch, {'COMPLETE'}, {'FAILED', 'STOPPED'}, 'sync')
    print(f"    documents indexed: {stats.get('numberOfNewDocumentsIndexed', 0)} new, "
          f"{stats.get('numberOfModifiedDocumentsIndexed', 0)} modified, "
          f"{stats.get('numberOfDocumentsFailed', 0)} failed")


def upsert_env(values):
    """Set KEY=value lines in .env, replacing existing ones."""
    lines = open(ENV_FILE, encoding='utf-8').read().splitlines() if os.path.exists(ENV_FILE) else []
    remaining = dict(values)
    for i, line in enumerate(lines):
        m = re.match(r'^\s*([A-Z_][A-Z0-9_]*)\s*=', line)
        if m and m.group(1) in remaining:
            lines[i] = f"{m.group(1)}={remaining.pop(m.group(1))}"
    lines += [f"{k}={v}" for k, v in remaining.items()]
    with open(ENV_FILE, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')


def main():
    print(f"Policy bucket : {config.POLICY_BUCKET}")
    print(f"Vector bucket : {config.VECTOR_STORE_BUCKET}")
    print(f"Service role  : {config.AGENTCORE_ROLE_ARN}\n")
    ids = {}
    for domain, name, prefix, index_attr, env_key in KNOWLEDGE_BASES:
        index_name = getattr(config, index_attr)
        print(f"{domain} KB ({name})")
        kb_id = find_kb(name)
        if kb_id:
            print(f"    exists: {kb_id}")
        else:
            kb_id = create_kb(name, domain, index_name)
            print(f"    created: {kb_id} -> index {index_name}")
        wait_for(lambda: (lambda kb: (kb['status'], kb.get('failureReasons')))(
                     agent.get_knowledge_base(knowledgeBaseId=kb_id)['knowledgeBase']),
                 {'ACTIVE'}, {'FAILED', 'DELETE_UNSUCCESSFUL'}, 'knowledge base')
        ds_id = ensure_data_source(kb_id, domain, prefix)
        print(f"    data source: {ds_id} (s3://{config.POLICY_BUCKET}/{prefix})")
        sync(kb_id, ds_id)
        ids[env_key] = kb_id
        print()

    upsert_env(ids)
    print("Written to .env:")
    for k, v in ids.items():
        print(f"  {k}={v}")
    print("\nNext: python tests/test_agent.py task5")


if __name__ == '__main__':
    main()