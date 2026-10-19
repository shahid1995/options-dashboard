from datetime import date, timedelta

def is_trial_active(started: date, today: date) -> bool:
    trial_ends = started + timedelta(days=14)
    return today <= trial_ends

def chargeable_days(started: date, today: date) -> int:
    if not is_trial_active(started, today):
        return (today - trial_ends_for(started)).days
    return 0

def trial_ends_for(started: date) -> date:
    return started + timedelta(days=14)
