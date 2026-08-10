# Event Log page
import gradio as gr

from database.repositories.event_repository import EventRepository


def create_page(global_state_component):
    """Create event log viewer page"""
    with gr.Blocks() as page:
        gr.Markdown("# Event Log")
        gr.Markdown("*View system events and instrumentation data*")

        # ZONE A: Filters + list (always visible)
        with gr.Row():
            with gr.Column(scale=3):
                event_type_filter = gr.Dropdown(
                    choices=["all", "task.*", "scenario.*", "agent.*", "watchdog.*"],
                    label="Event Type Filter",
                    value="all"
                )
            with gr.Column(scale=1):
                refresh_btn = gr.Button("Refresh", variant="secondary")

        event_list = gr.Dataframe(
            headers=["Timestamp", "Event Type", "Data"],
            label="Events",
            wrap=True
        )

        # Refresh timer (hidden background trigger)
        refresh_timer = gr.Timer(value=2, render=False)

        def load_events(event_type_filter):
            """Load events with optional type filter"""
            event_repo = EventRepository()

            if event_type_filter == "all":
                events = event_repo.find_recent(limit=100)
            else:
                events = event_repo.find_by_event_type_prefix(event_type_filter, limit=100)

            return [[str(e.timestamp), e.event_type, e.data] for e in events]

        # Event handlers
        refresh_timer.tick(lambda: load_events(event_type_filter.value), outputs=[event_list])
        refresh_btn.click(load_events, inputs=[event_type_filter], outputs=[event_list])

        # Load on page load
        page.load(load_events, inputs=[event_type_filter], outputs=[event_list])

    return page
