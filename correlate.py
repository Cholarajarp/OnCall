"""Deterministic deploy-vs-spike correlation. Pure function, no I/O, no model.

Aligns deployment timestamps with the measured error-rate series. The output is
a timing observation for the operator, never a root-cause claim: correlation
does not prove causation, and missing evidence yields 'insufficient_evidence'.
"""
from __future__ import annotations

from datetime import datetime, timedelta

SPIKE_RATE = 0.05          # same threshold as oncall_core.assess
LOOKBACK = timedelta(minutes=30)  # deploys older than this before the spike are not candidates


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _points(series):
    points = []
    for row in series or []:
        if not isinstance(row, dict):
            continue
        at, rate = _time(row.get('ts')), row.get('error_rate')
        if at and isinstance(rate, (int, float)) and 0 <= rate <= 1:
            points.append((at, float(rate)))
    return sorted(points)


def _deploys(deploys):
    rows = []
    for row in deploys or []:
        if not isinstance(row, dict):
            continue
        at = _time(row.get('deployed_at') or row.get('ts'))
        commit = row.get('commit') or row.get('commit_sha')
        if at and isinstance(commit, str):
            rows.append((at, commit, row))
    return sorted(rows, key=lambda item: item[0])


def _minutes(delta):
    return round(delta.total_seconds() / 60, 1)


def correlate(series, deploys):
    points, changes = _points(series), _deploys(deploys)
    result = {'verdict': 'insufficient_evidence', 'spike_start': None, 'candidate': None,
              'lead_minutes': None, 'peak_error_rate': None, 'baseline_error_rate': None,
              'summary': 'Not enough measured telemetry to correlate deployments with errors.'}
    if not points:
        return result
    # First sample of the most recent contiguous run above the threshold.
    start = None
    for index in range(len(points) - 1, -1, -1):
        if points[index][1] > SPIKE_RATE:
            start = index
        elif start is not None:
            break
    if start is None:
        return dict(result, verdict='no_spike', peak_error_rate=max(r for _, r in points),
                    summary='No error spike above 5% in the measured window.')
    spike_at = points[start][0]
    baseline = [r for _, r in points[:start]]
    result.update(spike_start=spike_at.isoformat(),
                  peak_error_rate=max(r for _, r in points[start:]),
                  baseline_error_rate=round(sum(baseline) / len(baseline), 4) if baseline else None)
    # Keep the caller's original timestamp string when available for exact matching.
    for row in series:
        if isinstance(row, dict) and _time(row.get('ts')) == spike_at:
            result['spike_start'] = row['ts']
            break
    before = [item for item in changes if spike_at - LOOKBACK <= item[0] <= spike_at + timedelta(minutes=2)]
    if not before:
        return dict(result, verdict='no_recent_deploy',
                    summary=f"Errors rose above 5% at {result['spike_start']} with no deployment in the preceding 30 minutes. Look beyond deployments: dependencies, traffic, configuration.")
    at, commit, row = before[-1]
    lead = _minutes(spike_at - at)
    candidate = {'commit': commit, 'deployed_at': row.get('deployed_at') or row.get('ts'),
                 'active': bool(row.get('active') or row.get('is_active'))}
    if lead >= 0:
        timing = f'{lead} minutes before'
    else:
        # Within one bucket after; the bucket start precedes the deploy timestamp.
        timing = 'within the same measurement bucket as'
    return dict(result, verdict='deploy_precedes_spike', candidate=candidate, lead_minutes=lead,
                summary=(f"Deployment {commit} went live {timing} errors rose above 5% "
                         f"(peak {round(result['peak_error_rate'] * 100, 1)}%). The timing fits, but it does not prove the deployment caused it."))
