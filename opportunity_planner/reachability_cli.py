from __future__ import annotations

import argparse
import json
from pathlib import Path

from .opportunities import ReachabilityAnalyzer, compare_reports
from .opportunities.adapters import from_schedule


def load_json(path: str):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def load_input(path: str, schedule: bool = False):
    data = load_json(path)
    if schedule:
        return from_schedule(data)
    return data


def write_json(path: str | None, payload: object) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding='utf-8')
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def analyze_command(args: argparse.Namespace) -> int:
    analyzer = ReachabilityAnalyzer()
    report = analyzer.analyze(load_input(args.fixture, args.schedule))
    payload = report.as_dict()
    if args.cohort:
        payload['cohort_detail'] = analyzer.cohort_detail(report, args.cohort)
    if args.person:
        payload['person_detail'] = analyzer.person_detail(report, args.person)
    if args.status:
        wanted = set(args.status.split(','))
        payload['filtered_paths'] = [
            item
            for item in payload['blocked_paths']
            if item['status'] in wanted or item['individual_status'] in wanted
        ]
    write_json(args.output, payload)
    return 0


def compare_command(args: argparse.Namespace) -> int:
    analyzer = ReachabilityAnalyzer()
    left = analyzer.analyze(load_input(args.left, args.schedule))
    right = analyzer.analyze(load_input(args.right, args.schedule))
    result = compare_reports(left, right)
    if args.course:
        result['by_course'] = {args.course: result['by_course'].get(args.course, {})}
    write_json(args.output, result)
    return 0


def register_commands(subparsers) -> None:
    reach = subparsers.add_parser(
        'reachability',
        help='analyse course -> interview -> shift reachability without changing anything',
    )
    reach.add_argument('--fixture', required=True)
    reach.add_argument('--output')
    reach.add_argument('--schedule', action='store_true', help='read a scheduler plan fixture instead of reachability input')
    reach.add_argument('--cohort', help='include every path for one cohort')
    reach.add_argument('--person', help='include every path for one person')
    reach.add_argument('--status', help='comma separated statuses for filtered_paths')
    reach.set_defaults(handler=analyze_command)

    compare = subparsers.add_parser(
        'compare-scenarios',
        help='compare downstream opportunity outcomes of two course schedules',
    )
    compare.add_argument('--left', required=True)
    compare.add_argument('--right', required=True)
    compare.add_argument('--output')
    compare.add_argument('--schedule', action='store_true', help='read scheduler plan fixtures instead')
    compare.add_argument('--course', help='restrict the printed course comparison')
    compare.set_defaults(handler=compare_command)
