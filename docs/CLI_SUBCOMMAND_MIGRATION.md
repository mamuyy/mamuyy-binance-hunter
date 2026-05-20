# CLI Subcommand Migration

The legacy flag commands still work. The new subcommands are aliases for the same safe code paths, so operators can migrate gradually without changing behavior.

## Command Mapping

| Old command | New command |
| --- | --- |
| `python main.py --health` | `python main.py health` |
| `python main.py --risk-check` | `python main.py risk-check` |
| `python main.py --health-guardian-once` | `python main.py health-guardian-once` |
| `python main.py --heartbeat-test` | `python main.py heartbeat-test` |
| `python main.py --shadow-analysis` | `python main.py shadow-analysis` |
| `python main.py --label-outcomes --days 7` | `python main.py label-outcomes --days 7` |
| `python main.py --backfill --days 7` | `python main.py backfill --days 7` |
| `python main.py --optimize-filters` | `python main.py optimize-filters` |
| `python main.py --fix-regime-labels` | `python main.py fix-regime-labels` |

## Safety Notes

- Existing flags remain supported for backwards compatibility.
- Subcommands route to the same existing handlers as the legacy flags.
- No broker execution is added by these CLI aliases.
- Trading logic, ML logic, paper execution, and orchestrator behavior are unchanged.
