"""Re-evaluate one saved report without searching, writing, or changing its artifacts."""
import argparse
import json
from pathlib import Path

from adra.configuration import RuntimeConfig
from adra.graph import evaluate_report
from adra.planning import Plan
from adra.verification import verify_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New evaluation JSON file')
    parser.add_argument('--max-model-calls', type=int, default=8)
    parser.add_argument('--verify-claims', action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output already exists; choose a new file')
    saved = json.loads(args.run.read_text(encoding='utf-8'))
    if saved.get('architecture') != 'global_research_v2':
        parser.error('Requires a global_research_v2 run; old metrics have different meanings')
    plan = Plan.model_validate(saved['plan'])
    config = RuntimeConfig.from_env(args.max_model_calls)
    policy = config.create_policy()
    # Never reuse judgments from a previous model or rubric run.
    result = evaluate_report({**saved, 'coverage': {}}, plan, policy)
    verification = verify_report(saved, policy) if args.verify_claims else {'status': 'not_requested'}
    artifact = {'claim_verification': verification, 'source_run': str(args.run.resolve()), 'target': 'final_report',
                'status': result['evaluation_status'], 'coverage': result['coverage'],
                'runtime_config': config.model_dump(), 'model_call_records': policy.call_records,
                'total_tokens': policy.tokens, 'token_usage_complete': policy.usage_complete}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as file:
        json.dump(artifact, file, ensure_ascii=False, indent=2)
    print(args.output)


if __name__ == '__main__':
    main()
