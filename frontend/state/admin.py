"""Admin inputs belong to one session and are removed when authorization is lost."""

import streamlit as st


def clear_admin_state() -> None:
    for key in list(st.session_state):
        if key.startswith("admin_"):
            st.session_state.pop(key, None)


def retain_authorized_state(user: dict) -> None:
    if user.get("role") != "admin":
        clear_admin_state()
    # Monitoring history, analytics and exports are available to every signed-in
    # role. A role change alone must not discard these ordinary analysis queries.
