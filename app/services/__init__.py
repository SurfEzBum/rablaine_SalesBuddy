"""
Services module for Sales Buddy.
Contains business logic services separate from routes.
"""

from app.services.revenue_import import (
    fiscal_month_to_date,
    get_import_history,
    get_months_in_database,
    get_customer_revenue_history,
)

from app.services.revenue_analysis import (
    AnalysisConfig,
    CustomerSignals,
    compute_signals,
    categorize_customer,
    determine_action,
    run_analysis_for_all,
    get_actionable_analyses,
    get_seller_alerts
)

__all__ = [
    # Revenue query functions
    'fiscal_month_to_date',
    'get_import_history',
    'get_months_in_database',
    'get_customer_revenue_history',
    # Analysis functions
    'AnalysisConfig',
    'CustomerSignals',
    'compute_signals',
    'categorize_customer',
    'determine_action',
    'run_analysis_for_all',
    'get_actionable_analyses',
    'get_seller_alerts'
]
