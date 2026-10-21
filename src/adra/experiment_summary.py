"""Descriptive statistics with explicit missingness; never synthesize failed scores."""
import random
import statistics
from collections import defaultdict

METRICS = ('semantic_coverage', 'supported_claim_rate', 'contradicted_claim_rate',
           'insufficient_claim_rate', 'total_tokens', 'tool_requests_sent', 'latency_seconds',
           'tool_rejections', 'reflection_calls')


def aggregate(records):
    groups = defaultdict(list)
    for record in records:
        groups[(record['case'], record.get('variant', 'event'))].append(record)
    output = []
    for (case, variant), runs in sorted(groups.items()):
        metrics = {}
        for name in METRICS:
            values = [r.get('metrics', {}).get(name) for r in runs]
            observed = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
            ci = None
            if len(observed) >= 2:
                rng = random.Random(0)
                estimates = sorted(statistics.mean(rng.choices(observed, k=len(observed)))
                                   for _ in range(1000))
                ci = [estimates[24], estimates[974]]
            metrics[name] = {'n': len(observed), 'missing': len(values) - len(observed),
                             'mean': statistics.mean(observed) if observed else None,
                             'sample_std': statistics.stdev(observed) if len(observed) > 1 else None,
                             'bootstrap_95_ci': ci}
        output.append({'case': case, 'variant': variant, 'runs': len(runs),
                       'failed_runs': sum(r['status'] != 'completed' for r in runs),
                       'incomplete_evaluations': sum(r.get('metrics', {}).get('evaluation_status') == 'incomplete' for r in runs),
                       'incomplete_claim_checks': sum(r.get('metrics', {}).get('claim_verification_status') == 'incomplete' for r in runs),
                       'metrics': metrics})
    return {'groups': output, 'method': 'within-case/variant descriptive statistics; percentile bootstrap, seed=0, 1000 resamples',
            'limitations': 'Small repeat counts yield unstable intervals. Missing scores and failed runs are reported, not imputed. No causal or significance claim.'}
