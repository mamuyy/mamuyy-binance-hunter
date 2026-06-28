# CP-041 Ranking / EV / Lifecycle Pivot Audit

Verdict: **REVIEW**  
Phase 3: **LOCKED**  
Classifier gate: **FROZEN**  
Model promotion: **HOLD**  
PAPER_ONLY: **true**

Rows: 1043

Best top-k signal: `{'mode': 'global', 'top_k': 1, 'sample_count': 1, 'top_k_rows': 1, 'top_k_win_rate': 1.0, 'top_k_loss_rate': 0.0, 'top_k_avg_profit': 27.380839, 'rest_rows': 1042, 'rest_win_rate': 0.46833013435700577, 'rest_loss_rate': 0.5316698656429942, 'rest_avg_profit': 1.7968729145873317, 'top_vs_rest_winrate_delta': 0.5316698656429942, 'top_vs_rest_profit_delta': 25.58396608541267}`

Best loss avoidance threshold: `{'threshold': '>=95', 'kept_rows': 107, 'filtered_rows': 936, 'kept_win_rate': 0.7383177570093458, 'kept_loss_rate': 0.2616822429906542, 'filtered_win_rate': 0.43803418803418803, 'filtered_loss_rate': 0.561965811965812, 'loss_avoidance_delta': 0.3002835689751578, 'profit_delta_kept_vs_filtered': 7.317492668314162, 'kept_source_distribution': {'internal_paper_trades': 104, 'historical_outcomes': 3}, 'filtered_source_distribution': {'historical_outcomes': 594, 'internal_paper_trades': 342}}`
