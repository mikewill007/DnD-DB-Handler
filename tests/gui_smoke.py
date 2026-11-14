"""Interactive-environment smoke test; uses temporary databases, never real decisions.

Run explicitly with: python tests/gui_smoke.py
"""
from pathlib import Path
import sys
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_resolutions import ResolutionTests
from normalizer.conflict_browser import ConflictBrowser


def main():
    """Drive real Tk callbacks against temporary fixtures without opening user data."""
    fixture = ResolutionTests()
    fixture.setUp()
    root = None
    try:
        store, group = fixture.prepare()
        root = tk.Tk()
        # Keep the test window hidden; update() still processes Tk selection and
        # modified-text events as it would during an interactive session.
        root.withdraw()
        app = ConflictBrowser(root, store)
        root.update()
        assert len(app.tree.get_children()) == 1
        app.tree.selection_set(group['key'])
        root.update()
        assert app.current == group['key']
        assert len(app.source_records) == 3
        by_cost = {i['data']['cost_value']: i for i in group['items']}
        # Draft composition and on-demand source inspection.
        app.preferred.set(by_cost[15]['id'])
        app.choose_preferred()
        app.choose_field('weight_lb', by_cost[50]['id'])
        root.update()
        assert app.dirty
        assert '"weight_lb": 5' in app.preview.get('1.0', 'end')
        app.source_selector.current(0)
        app.show_source()
        assert 'custom_ability' in app.source_text.get('1.0', 'end')
        app.note.insert('1.0', 'Combined in GUI smoke test')
        root.update()
        # Exercise the actual buttons so storage and UI state are tested together.
        app.save_button.invoke()
        root.update()
        assert not app.dirty
        assert store.groups[group['key']]['status'] == 'Resolved'
        assert '1 / 1' in app.progress.cget('text')
        app.status_filter.set('All groups')
        app.refresh_list()
        root.update()
        app.undo_button.invoke()
        root.update()
        assert store.groups[group['key']]['status'] == 'Unresolved'
        app.separate_button.invoke()
        root.update()
        assert store.groups[group['key']]['status'] == 'Kept separate'
        # Filtering and refresh must still work after repeated save/undo actions.
        app.search.set('nonexistent item')
        root.update()
        assert not app.tree.get_children()
        app.search.set('Longsword')
        app.status_filter.set('Kept separate')
        app.refresh_list()
        root.update()
        assert len(app.tree.get_children()) == 1
        app.reload()
        root.update()
        print('GUI smoke passed: comparison, field choice, source JSON, save, undo, keep separate, search, filters, refresh')
    finally:
        if root is not None:
            root.destroy()
        fixture.tearDown()


if __name__ == '__main__':
    main()
