"""Wave 3 Run read helpers. Execution/control arrive with the Wave 4 Runner."""


def allowed_actions(_run):
    """Do not advertise pause/resume/cancel before an executor can honor them."""
    return []
