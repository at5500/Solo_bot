from core.tasks.cron_tasks import (
    AUDIT_DRAIN_TRIGGER,
    DAILY_STATS_REPORT_TRIGGER,
    STALE_PAYMENTS_SWEEP_TRIGGER,
    scheduled_audit_drain,
    scheduled_audit_drain_process_runner,
    scheduled_stats_report,
    scheduled_stats_report_process_runner,
    sweep_stale_payments_job,
    sweep_stale_payments_process_runner,
)
from core.tasks.loop_tasks import (
    backup_loop,
    backup_thread_loop,
    notifications_loop,
    scheduled_broadcasts_loop_task,
    server_checks_loop,
)
from core.tasks.periodic_manager import periodic_task_manager


_TASKS_REGISTERED = False


def register_periodic_tasks() -> None:
    global _TASKS_REGISTERED
    if _TASKS_REGISTERED:
        return
    from config import PROCESS_POOL_SIZE

    process_budget = max(0, int(PROCESS_POOL_SIZE) if int(PROCESS_POOL_SIZE) > 1 else 0)

    if process_budget > 0:
        periodic_task_manager.register_process_loop_task("notifications", notifications_loop)
        process_budget -= 1
    else:
        periodic_task_manager.register_loop_task("notifications", notifications_loop)

    if process_budget > 0:
        periodic_task_manager.register_process_loop_task("scheduled_broadcasts", scheduled_broadcasts_loop_task)
        process_budget -= 1
    else:
        periodic_task_manager.register_loop_task("scheduled_broadcasts", scheduled_broadcasts_loop_task)

    if process_budget > 0:
        periodic_task_manager.register_process_loop_task("backup", backup_loop)
        process_budget -= 1
    else:
        periodic_task_manager.register_thread_loop_task("backup", backup_thread_loop)

    if process_budget > 0:
        periodic_task_manager.register_process_loop_task("server_checks", server_checks_loop)
        process_budget -= 1
    else:
        periodic_task_manager.register_loop_task("server_checks", server_checks_loop)

    periodic_task_manager.set_scheduler_process_workers(process_budget)

    if process_budget > 0:
        periodic_task_manager.register_cron_task(
            "audit_drain_midnight",
            scheduled_audit_drain_process_runner,
            AUDIT_DRAIN_TRIGGER,
            execution_mode="process",
        )
    else:
        periodic_task_manager.register_cron_task(
            "audit_drain_midnight",
            scheduled_audit_drain,
            AUDIT_DRAIN_TRIGGER,
        )

    if process_budget > 0:
        periodic_task_manager.register_cron_task(
            "daily_stats_report",
            scheduled_stats_report_process_runner,
            DAILY_STATS_REPORT_TRIGGER,
            execution_mode="process",
        )
    else:
        periodic_task_manager.register_cron_task(
            "daily_stats_report",
            scheduled_stats_report,
            DAILY_STATS_REPORT_TRIGGER,
        )

    if process_budget > 0:
        periodic_task_manager.register_cron_task(
            "sweep_stale_payments",
            sweep_stale_payments_process_runner,
            STALE_PAYMENTS_SWEEP_TRIGGER,
            execution_mode="process",
        )
    else:
        periodic_task_manager.register_cron_task(
            "sweep_stale_payments",
            sweep_stale_payments_job,
            STALE_PAYMENTS_SWEEP_TRIGGER,
        )

    _TASKS_REGISTERED = True
