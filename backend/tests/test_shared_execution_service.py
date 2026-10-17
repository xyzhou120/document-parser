from copy import deepcopy

from app.services.shared_execution_service import semantic_identity


def test_semantic_key_covers_every_reuse_boundary():
    base = dict(file_hash='file', content_digest='content', coverage_digest='coverage',
        preprocess_version='preprocess', contract_hash='contract', model_signature='model',
        field_contracts=[('pressure', 'rule')], strategy='page_batch_v1',
        prompt_version='prompt', validator_version='validator', token_limit=12000)
    original = semantic_identity(**base)
    for key in base:
        changed = deepcopy(base)
        changed[key] = [('pressure', 'changed')] if key == 'field_contracts' else (
            24000 if key == 'token_limit' else str(base[key]) + '-changed')
        assert semantic_identity(**changed) != original, key
    base['field_contracts'] = [('b', '2'), ('a', '1')]
    reverse = {**base, 'field_contracts': list(reversed(base['field_contracts']))}
    assert semantic_identity(**base) == semantic_identity(**reverse)
