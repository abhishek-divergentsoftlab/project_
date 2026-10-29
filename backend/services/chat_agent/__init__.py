"""The chat agent: one model loop that decides what the user wants; code only validates what it does.

``engine.run_turn`` is the entry point; ``tools`` holds what the model may call, ``guards`` the grounding and argument
cleaning, ``state`` what is remembered between turns, ``prompt`` the system prompt.
"""
