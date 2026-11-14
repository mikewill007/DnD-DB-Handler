"""Tk desktop UI for comparing and resolving groups of conflicting items.

ReviewStore owns persistence and stale-data checks. This module owns selection,
unsaved field choices, and display; changing a radio button or field choice has
no database effect until the user saves a resolution.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .resolutions import ReviewStore
from .schema import FIELDS

PROJECT = Path(__file__).resolve().parents[1]
PALETTES = {
    'Light': dict(background='#f5f6f8', surface='#ffffff', text='#17212b',
                  muted='#526174', changed='#fff1cc', chosen='#dff3e6',
                  accent='#245c99', selected_text='#ffffff', border='#b4bcc8'),
    'Dark': dict(background='#20252d', surface='#2b323d', text='#f1f4f8',
                 muted='#bac6d6', changed='#59451e', chosen='#224b38',
                 accent='#9ecbff', selected_text='#17212b', border='#667386'),
}


def display(value):
    """Format values while keeping unknown data visually distinct from false/zero."""
    if value is None:
        return '— missing —'
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


class ConflictBrowser:
    """Compare a whole conflict group and manage its unsaved resolution draft."""
    def __init__(self, root, store, theme='Light'):
        self.root, self.store = root, store
        self.current = None
        # Field overrides map a normalized field to its donor variant ID. They are
        # layered over the preferred variant; dirty tracks the unsaved draft only.
        self.field_choices = {}
        self.dirty = False
        self.visible_keys = []
        self.search = tk.StringVar()
        self.status_filter = tk.StringVar(value='Needs attention')
        self.edition_filter = tk.StringVar(value='All editions')
        self.preferred = tk.StringVar()
        self.only_differences = tk.BooleanVar(value=True)
        self.notice = tk.StringVar(value='Choose a conflict group to begin.')
        self.theme = tk.StringVar(value=theme)
        self.root.title('D&D Conflict Review')
        self.root.geometry('1440x900')
        self.root.minsize(1050, 700)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.apply_theme()
        self._layout()
        self.refresh_list()
        if self.visible_keys:
            self.tree.selection_set(self.visible_keys[0])
        self.root.bind('<Control-f>', lambda event: self.search_entry.focus_set())
        self.root.bind('<Control-s>', lambda event: self.save('prefer'))

    def apply_theme(self, event=None):
        """Use paired colors, independent of native macOS dark-mode defaults."""
        colors = self.colors = PALETTES[self.theme.get()]
        style = ttk.Style(self.root)
        # Native themes can ignore explicit colors on macOS and Windows.
        style.theme_use('clam')
        style.configure('.', background=colors['background'], foreground=colors['text'],
                        fieldbackground=colors['surface'], bordercolor=colors['border'],
                        lightcolor=colors['border'], darkcolor=colors['border'])
        style.map('.', background=[('active', colors['surface'])],
                  foreground=[('disabled', colors['muted'])])
        style.configure('Title.TLabel', font=('Segoe UI', 18, 'bold'))
        style.configure('Heading.TLabel', font=('Segoe UI', 11, 'bold'))
        style.configure('Muted.TLabel', foreground=colors['muted'])
        style.configure('Changed.TLabel', background=colors['changed'], foreground=colors['text'], padding=8)
        style.configure('Selected.TLabel', background=colors['chosen'], foreground=colors['text'], padding=8)
        style.configure('Treeview', background=colors['surface'], fieldbackground=colors['surface'])
        for widget in ('Treeview', 'TEntry', 'TCombobox'):
            style.map(widget, background=[('selected', colors['accent'])],
                      foreground=[('selected', colors['selected_text'])],
                      selectbackground=[('!disabled', colors['accent'])],
                      selectforeground=[('!disabled', colors['selected_text'])])
        style.map('TCombobox', fieldbackground=[('readonly', colors['surface'])],
                  foreground=[('readonly', colors['text'])])
        style.map('TNotebook.Tab', background=[('selected', colors['surface'])])
        self.root.configure(background=colors['background'])
        # These defaults also cover new comparison text boxes and dropdown lists.
        for option, value in (
            ('*Text.background', colors['surface']), ('*Text.foreground', colors['text']),
            ('*Text.insertBackground', colors['text']), ('*Text.selectBackground', colors['accent']),
            ('*Text.selectForeground', colors['selected_text']),
            ('*Listbox.background', colors['surface']), ('*Listbox.foreground', colors['text']),
            ('*Listbox.selectBackground', colors['accent']),
            ('*Listbox.selectForeground', colors['selected_text']),
        ):
            self.root.option_add(option, value)
        if hasattr(self, 'canvas'):
            self.canvas.configure(background=colors['background'])
            for widget in (self.source_text, self.preview, self.note):
                widget.configure(background=colors['surface'], foreground=colors['text'],
                                 insertbackground=colors['text'], selectbackground=colors['accent'],
                                 selectforeground=colors['selected_text'])
            self.render_comparison()

    # Window layout and widgets.

    def _layout(self):
        """Create widgets once; group selection repopulates their contents later."""
        header = ttk.Frame(self.root, padding=(16, 12))
        header.pack(fill='x')
        ttk.Label(header, text='D&D Conflict Review', style='Title.TLabel').pack(side='left')
        ttk.Button(header, text='Refresh', command=self.reload).pack(side='right')
        theme_picker = ttk.Combobox(header, textvariable=self.theme, values=tuple(PALETTES),
                                   state='readonly', width=7)
        theme_picker.pack(side='right', padx=(4, 12))
        theme_picker.bind('<<ComboboxSelected>>', self.apply_theme)
        ttk.Label(header, text='Theme').pack(side='right')
        self.progress = ttk.Label(header, style='Muted.TLabel')
        self.progress.pack(side='right', padx=20)
        panes = ttk.Panedwindow(self.root, orient='horizontal')
        panes.pack(fill='both', expand=True, padx=12)
        # Searchable conflict list and review-progress controls.
        sidebar = ttk.Frame(panes, padding=6, width=355)
        panes.add(sidebar, weight=0)
        ttk.Label(sidebar, text='Find an item', style='Heading.TLabel').pack(anchor='w', pady=(0, 6))
        self.search_entry = ttk.Entry(sidebar, textvariable=self.search)
        self.search_entry.pack(fill='x')
        self.search.trace_add('write', lambda *args: self.refresh_list())
        filters = ttk.Frame(sidebar)
        filters.pack(fill='x', pady=8)
        for var, values, width in (
            (self.status_filter, ('Needs attention', 'All groups', 'Resolved', 'Kept separate', 'Needs review'), 18),
            (self.edition_filter, ('All editions', '2014', '2024', 'unknown'), 12),
        ):
            box = ttk.Combobox(filters, textvariable=var, values=values, state='readonly', width=width)
            box.pack(side='left', padx=(0, 4))
            box.bind('<<ComboboxSelected>>', lambda event: self.refresh_list())
        list_frame = ttk.Frame(sidebar)
        list_frame.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(list_frame, columns=('edition', 'status'), selectmode='browse')
        self.tree.heading('#0', text='Item')
        self.tree.heading('edition', text='Edition')
        self.tree.heading('status', text='Status')
        self.tree.column('#0', width=165, minwidth=120)
        self.tree.column('edition', width=52, stretch=False)
        self.tree.column('status', width=94, stretch=False)
        scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.select_group)
        self.count_label = ttk.Label(sidebar, style='Muted.TLabel')
        self.count_label.pack(anchor='w', pady=6)
        right = ttk.Frame(panes, padding=(12, 6))
        panes.add(right, weight=1)
        self.title = ttk.Label(right, text='Compare conflicting records', style='Title.TLabel')
        self.title.pack(anchor='w')
        self.subtitle = ttk.Label(right, text='Select an item on the left.', style='Muted.TLabel')
        self.subtitle.pack(anchor='w', pady=(4, 8))
        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill='both', expand=True)
        # A two-axis canvas supports groups with more variants than fit on screen.
        compare = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(compare, text='Compare records')
        controls = ttk.Frame(compare)
        controls.pack(fill='x', pady=(0, 8))
        ttk.Checkbutton(controls, text='Show differing fields only', variable=self.only_differences,
                        command=self.render_comparison).pack(side='left')
        ttk.Label(controls, text='Gold = different · Green = chosen value', style='Muted.TLabel').pack(side='right')
        comparison = ttk.Frame(compare)
        comparison.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(comparison, highlightthickness=0, background=self.colors['background'])
        vertical = ttk.Scrollbar(comparison, orient='vertical', command=self.canvas.yview)
        horizontal = ttk.Scrollbar(comparison, orient='horizontal', command=self.canvas.xview)
        self.canvas.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
        vertical.pack(side='right', fill='y')
        horizontal.pack(side='bottom', fill='x')
        self.canvas.pack(fill='both', expand=True)
        self.table = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.table, anchor='nw')
        self.table.bind('<Configure>', lambda event: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        # Wheel scrolling is scoped to the comparison area, leaving text editors usable.
        self.canvas.bind('<MouseWheel>', lambda e: self.canvas.yview_scroll(-int(e.delta / 120), 'units'))
        # Full source payloads are loaded on demand to keep group navigation light.
        source_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(source_tab, text='Original data & attribution')
        ttk.Label(source_tab, text='Choose a provider record to inspect its complete original JSON.').pack(anchor='w')
        self.source_selector = ttk.Combobox(source_tab, state='readonly')
        self.source_selector.pack(fill='x', pady=8)
        self.source_selector.bind('<<ComboboxSelected>>', self.show_source)
        self.source_text = ScrolledText(source_tab, wrap='word', font=('Consolas', 10))
        self.source_text.pack(fill='both', expand=True)
        self.source_text.configure(state='disabled')
        self.source_records = []
        decision_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(decision_tab, text='Resolution preview')
        self.preview = ScrolledText(decision_tab, wrap='word', font=('Consolas', 10))
        self.preview.pack(fill='both', expand=True)
        self.preview.configure(state='disabled')
        # Notes and save/undo actions stay visible beneath all comparison tabs.
        note_frame = ttk.Frame(right)
        note_frame.pack(fill='x', pady=(8, 4))
        ttk.Label(note_frame, text='Review note (optional)').pack(anchor='w')
        self.note = tk.Text(note_frame, height=2, wrap='word', font=('Segoe UI', 10), relief='solid', borderwidth=1)
        self.note.pack(fill='x', pady=(3, 0))
        self.note.bind('<<Modified>>', self.note_changed)
        actions = ttk.Frame(right)
        actions.pack(fill='x', pady=8)
        self.save_button = ttk.Button(actions, text='Save preferred / combined record', command=lambda: self.save('prefer'))
        self.save_button.pack(side='left')
        self.separate_button = ttk.Button(actions, text='Keep all separate', command=lambda: self.save('separate'))
        self.separate_button.pack(side='left', padx=8)
        self.undo_button = ttk.Button(actions, text='Undo saved decision', command=self.undo)
        self.undo_button.pack(side='right')
        for button in (self.save_button, self.separate_button, self.undo_button):
            button.configure(state='disabled')
        ttk.Label(self.root, textvariable=self.notice, padding=(18, 9), relief='sunken', anchor='w').pack(fill='x')

    # Filtering, navigation, and loading a review draft.

    def refresh_list(self, select_key=None):
        """Filter the cached catalog without querying or changing the database."""
        selected = select_key or self.current
        self.tree.delete(*self.tree.get_children())
        self.visible_keys = []
        query = self.search.get().casefold().strip()
        for key, group in self.store.groups.items():
            status = group['status']
            choice = self.status_filter.get()
            if choice == 'Needs attention' and status not in ('Unresolved', 'Needs review'):
                continue
            if choice not in ('Needs attention', 'All groups') and choice != status:
                continue
            if self.edition_filter.get() not in ('All editions', group['edition']):
                continue
            haystack = f"{group['name']} {group['ruleset']} {group['record_type']}".casefold()
            if query and query not in haystack:
                continue
            self.tree.insert('', 'end', iid=key, text=group['name'], values=(group['edition'], status))
            self.visible_keys.append(key)
        counts = Counter(g['status'] for g in self.store.groups.values())
        completed = counts['Resolved'] + counts['Kept separate']
        self.progress.configure(text=f'{completed} / {len(self.store.groups)} groups reviewed')
        self.count_label.configure(text=f'{len(self.visible_keys)} groups shown')
        if selected in self.visible_keys:
            self.tree.selection_set(selected)
            self.tree.see(selected)

    def can_leave(self):
        """Guard navigation/closing when the current draft has unsaved edits."""
        return not self.dirty or messagebox.askyesno('Unsaved choices',
            'Discard your unsaved choices for this item?', parent=self.root)

    def select_group(self, event=None):
        selection = self.tree.selection()
        if not selection or selection[0] == self.current:
            return
        if not self.can_leave():
            if self.current in self.visible_keys:
                self.tree.selection_set(self.current)
            return
        self.load_group(selection[0])

    def load_group(self, key):
        """Populate a group's controls from current records and any saved decision."""
        self.current = key
        group = self.store.groups[key]
        self.title.configure(text=group['name'])
        self.subtitle.configure(text=f"{group['edition']} · {group['record_type'].replace('_', ' ')} · "
            f"{group['ruleset']} · {len(group['items'])} variants · {group['pairs']} conflict pairs · {group['status']}")
        decision = group.get('decision') or {}
        ids = {i['id'] for i in group['items']}
        self.preferred.set(decision.get('preferred_id') if decision.get('preferred_id') in ids else group['items'][0]['id'])
        # A stale decision may refer to variants removed by a rebuild. Restore only
        # choices that still have donors; saving still requires the current snapshot.
        self.field_choices = {f: uid for f, uid in decision.get('field_choices', {}).items() if uid in ids and f in FIELDS}
        self.note.delete('1.0', 'end')
        self.note.insert('1.0', decision.get('note', ''))
        self.note.edit_modified(False)
        self.dirty = False
        self.render_comparison()
        self.source_records = [(index, s) for index, item in enumerate(group['items'], 1) for s in item['sources']]
        labels = [f"Variant {n} · {s['source_id']} · {s['collection']} · {s['external_id']} {s['json_pointer']}"
                  for n, s in self.source_records]
        self.source_selector.configure(values=labels)
        self.source_selector.set('Choose a source record…')
        self.set_text(self.source_text, 'Original JSON loads when you choose a source above.')
        self.save_button.configure(state='normal')
        self.separate_button.configure(state='normal')
        self.undo_button.configure(state='normal' if decision else 'disabled')
        self.notice.set('Choose a preferred variant, then use other field values as needed. Save when ready.'
                        if group['status'] != 'Needs review' else
                        'Source data changed since your last decision. Review the current values and save again.')

    # Comparison rendering and field selection (in-memory draft only).

    def render_comparison(self):
        """Render variants as columns and shared equipment fields as rows."""
        for widget in self.table.winfo_children():
            widget.destroy()
        if self.current is None:
            return
        group = self.store.groups[self.current]
        items = group['items']
        ttk.Label(self.table, text='FIELD', width=23, padding=10, style='Heading.TLabel').grid(row=0, column=0, sticky='nw')
        for col, item in enumerate(items, 1):
            header = ttk.Frame(self.table, padding=10)
            header.grid(row=0, column=col, sticky='nsew', padx=2)
            ttk.Radiobutton(header, text=f'Variant {col} — preferred', variable=self.preferred,
                            value=item['id'], command=self.choose_preferred).pack(anchor='w')
            providers = ', '.join(sorted({s['source_id'] for s in item['sources']}))
            ttk.Label(header, text=providers, wraplength=285, style='Heading.TLabel').pack(anchor='w', pady=5)
            ttk.Label(header, text=f"{len(item['sources'])} source record(s) · {item['id'][:8]}",
                      style='Muted.TLabel').pack(anchor='w')
        row = 1
        for field in FIELDS:
            values = [i['data'].get(field) for i in items]
            if all(v is None for v in values):
                continue
            differs = len({json.dumps(v, sort_keys=True) for v in values}) > 1
            if self.only_differences.get() and not differs:
                continue
            # An explicit field donor takes precedence over the preferred variant.
            selected = self.field_choices.get(field, self.preferred.get())
            name = field.replace('_', ' ').title()
            if field == 'weight_lb':
                name = 'Weight (lb)'
            label = ttk.Frame(self.table, padding=8)
            label.grid(row=row, column=0, sticky='nw')
            ttk.Label(label, text=name, style='Heading.TLabel', wraplength=160).pack(anchor='w')
            chosen = next(n for n, i in enumerate(items, 1) if i['id'] == selected)
            ttk.Label(label, text=f'Using variant {chosen}', style='Muted.TLabel').pack(anchor='w', pady=4)
            for col, (item, value) in enumerate(zip(items, values), 1):
                cell = ttk.Frame(self.table, padding=4)
                cell.grid(row=row, column=col, sticky='nsew', padx=2, pady=2)
                chosen_value = item['id'] == selected
                color = self.colors['chosen' if chosen_value else 'changed' if differs else 'surface']
                if field == 'description':
                    box = ScrolledText(cell, width=37, height=12, wrap='word', font=('Segoe UI', 10),
                                       background=color, foreground=self.colors['text'], relief='flat', padx=8, pady=8)
                    box.pack(fill='both', expand=True)
                    box.insert('1.0', display(value))
                    box.configure(state='disabled')
                else:
                    ttk.Label(cell, text=display(value), wraplength=285, width=39,
                              style='Selected.TLabel' if chosen_value else 'Changed.TLabel' if differs else 'TLabel',
                              padding=8).pack(fill='both', expand=True)
                if differs:
                    # Bind loop values now; deferred callbacks otherwise see the
                    # field and variant from the final iteration of the loop.
                    ttk.Button(cell, text='Using this value' if chosen_value else 'Use this value',
                        command=lambda f=field, uid=item['id']: self.choose_field(f, uid)).pack(anchor='w', pady=(4, 0))
            row += 1
        if row == 1:
            ttk.Label(self.table, text='No differing normalized fields. Show all fields or inspect the original JSON.',
                      padding=14).grid(row=1, column=0, columnspan=len(items) + 1)
        self.update_preview()

    def choose_preferred(self):
        """Start a fresh field selection from the newly preferred variant."""
        self.field_choices = {}
        self.dirty = True
        self.render_comparison()
        self.notice.set('Preferred variant changed. Save to apply your choices.')

    def choose_field(self, field, uid):
        self.field_choices[field] = uid
        self.dirty = True
        position = self.canvas.yview()[0]
        self.render_comparison()
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(position)
        self.notice.set(f"Selected {field.replace('_', ' ')}. Save to apply your choices.")

    def update_preview(self):
        """Show the same preferred-record-plus-field-overrides model used on save."""
        group = self.store.groups[self.current]
        items = {i['id']: i for i in group['items']}
        selected = items[self.preferred.get()]['data'].copy()
        for field, uid in self.field_choices.items():
            selected[field] = items[uid]['data'].get(field)
        preface = ('Saving a preferred / combined record creates one equipment entry with all source references.\n'
                   'Keeping all separate retains every variant and marks this group reviewed.\n\n')
        self.set_text(self.preview, preface + json.dumps(selected, ensure_ascii=False, indent=2))

    @staticmethod
    def set_text(widget, value):
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', value)
        widget.configure(state='disabled')

    # Source inspection and persistence actions.

    def show_source(self, event=None):
        """Load the selected provider record's original JSON and attribution."""
        index = self.source_selector.current()
        if index < 0:
            return
        _, source = self.source_records[index]
        try:
            original, attribution = self.store.original(source)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.error(exc)
            return
        metadata = {k: source[k] for k in ('source_id', 'collection', 'collection_edition', 'external_id',
                                          'source_file', 'source_url', 'json_pointer', 'document_key')}
        self.set_text(self.source_text, 'SOURCE REFERENCE\n' + json.dumps(metadata, ensure_ascii=False, indent=2) +
            '\n\nATTRIBUTION\n' + json.dumps(attribution, ensure_ascii=False, indent=2) +
            '\n\nNORMALIZED PROVIDER RECORD\n' + json.dumps(json.loads(source['normalized_json']), ensure_ascii=False, indent=2) +
            '\n\nORIGINAL JSON\n' + json.dumps(original, ensure_ascii=False, indent=2))

    def note_changed(self, event=None):
        # Resetting Tk's modified flag also emits this event; only true means an edit.
        if self.note.edit_modified():
            if self.current:
                self.dirty = True
            self.note.edit_modified(False)

    def save(self, mode):
        """Persist the draft, then move to the next group left in the current filter."""
        if self.current is None:
            return
        group = self.store.groups[self.current]
        key = self.current
        try:
            self.root.configure(cursor='watch')
            self.root.update_idletasks()
            self.store.save(key, group['fingerprint'], mode, self.preferred.get(), self.field_choices,
                            self.note.get('1.0', 'end-1c'))
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.error(exc)
            # save() rereads the database before writing. Display changed data
            # before the user can try to save again.
            if key in self.store.groups and group['fingerprint'] != self.store.groups[key]['fingerprint']:
                self.load_group(key)
            elif key not in self.store.groups:
                self.current = None
                self.dirty = False
                for button in (self.save_button, self.separate_button, self.undo_button):
                    button.configure(state='disabled')
            return
        finally:
            self.root.configure(cursor='')
        self.dirty = False
        # A saved group normally disappears from "Needs attention". Keep the list
        # position so reviewing the next item does not jump back to the beginning.
        old_keys = self.visible_keys[:]
        self.current = None
        self.refresh_list()
        if key in self.visible_keys:
            next_key = key
        elif self.visible_keys:
            index = old_keys.index(key) if key in old_keys else 0
            next_key = self.visible_keys[min(index, len(self.visible_keys) - 1)]
        else:
            next_key = key
        self.load_group(next_key)
        if next_key in self.visible_keys:
            self.tree.selection_set(next_key)
            self.tree.see(next_key)
        self.notice.set('Decision saved. Your equipment view is updated. This choice will be kept when you rebuild.')

    def undo(self):
        if self.current is None:
            return
        key = self.current
        try:
            self.store.undo(key)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.error(exc)
            return
        self.dirty = False
        self.refresh_list(key)
        self.load_group(key)
        self.notice.set('Saved decision removed. The original variants are visible again.')

    def reload(self):
        """Refresh after external changes, discarding draft edits only with consent."""
        if not self.can_leave():
            return
        try:
            self.store.refresh()
            self.store.sync()
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.error(exc)
            return
        self.dirty = False
        key = self.current
        self.refresh_list(key)
        if key in self.store.groups:
            self.load_group(key)
        elif self.visible_keys:
            self.load_group(self.visible_keys[0])
        else:
            self.current = None
            self.title.configure(text='No conflict groups available')
            self.subtitle.configure(text='Refresh after building a master database with conflicts.')
            self.render_comparison()
            for button in (self.save_button, self.separate_button, self.undo_button):
                button.configure(state='disabled')

    def error(self, exc):
        self.notice.set(f'Could not complete the action: {exc}')
        messagebox.showerror('Conflict review', str(exc), parent=self.root)

    def close(self):
        if self.can_leave():
            self.root.destroy()


# Command-line entry point (also used by review_conflicts.py).


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=PROJECT / 'output/dnd_master.sqlite')
    parser.add_argument('--resolutions', type=Path, help='Decision file (default: beside the master database)')
    parser.add_argument('--theme', choices=('light', 'dark'), default='light', help='Window colors (default: light)')
    args = parser.parse_args()
    try:
        store = ReviewStore(args.database, args.resolutions)
        root = tk.Tk()
        ConflictBrowser(root, store, theme=args.theme.title())
        root.mainloop()
    except (OSError, ValueError, sqlite3.Error, tk.TclError) as exc:
        print(f'Cannot open conflict browser: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
