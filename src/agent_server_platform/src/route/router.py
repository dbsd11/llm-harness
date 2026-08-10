# Gradio multi-page router
import gradio as gr


def make_gr_route():
    """Create Gradio routes for all pages"""
    # Human Agent identity persistence — injected once into <head> on the root Blocks.
    # gr.HTML <script> doesn't execute (Svelte @html = innerHTML); head= does.
    # The JS auto-persists the identity input (#ha-identity) value to localStorage.
    from pages.human_agent import inject_head

    with gr.Blocks(
        title="Agent Server Platform",
        theme=gr.themes.Base(),
        head=inject_head(),
        css="""
            @import url('https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css');

            #component-0 {
                padding: 20px;
                font-family: system-ui, -apple-system, sans-serif;
            }
            .nav-holder {
                display: flex;
            }
            .action-btn {
                transition: all 0.2s ease;
            }
            .action-btn:hover {
                opacity: 0.8;
                transform: translateY(-1px);
            }
        """,
    ) as demo:
        from pages.state import GlobalState
        global_state_component = gr.State(value=GlobalState())

        # Home page (default)
        from pages.home import create_page
        page = create_page(global_state_component)

    # Route configuration (Home is already the default page inside gr.Blocks)
    routes = [
        {"title": "Scenario Dashboard", "path": "/scenario_dashboard", "module": "scenario_dashboard", "show_in_navbar": True},
        {"title": "Task Monitor", "path": "/task_monitor", "module": "task_monitor", "show_in_navbar": True},
        {"title": "Agent Registry", "path": "/agent_registry", "module": "agent_registry", "show_in_navbar": True},
        {"title": "Execution Servers", "path": "/execution_servers", "module": "execution_servers", "show_in_navbar": True},
        {"title": "My Human Tasks", "path": "/human_tasks", "module": "human_tasks", "show_in_navbar": True},
        {"title": "Event Log", "path": "/event_log", "module": "event_log", "show_in_navbar": True},
    ]

    for route_config in routes:
        with demo.route(route_config["title"], route_config["path"], route_config["show_in_navbar"]) as route_page:
            from pages.state import GlobalState
            global_state_component = gr.State(value=GlobalState())

            from importlib import import_module
            page_module = import_module(f"pages.{route_config['module']}")
            page = page_module.create_page(global_state_component)

    # The Human Agent static route is mounted in start_gradio's lifespan handler
    # after launch() rebuilds demo.app (a pre-launch mount would be discarded
    # by App.create_app).
    return demo
