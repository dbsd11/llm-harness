# Global state for Gradio pages


class GlobalState:
    """Global state shared across Gradio pages"""

    def __init__(self):
        self.username = None
        self.user_id = None


def get_global_state(global_state):
    """Get global state (placeholder for auth integration)"""
    return global_state, ""
