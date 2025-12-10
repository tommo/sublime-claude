# Sublime Text API Reference - Key Classes and Methods

## Window Class
The `Window` class represents an open editor window and provides methods for file management, view control, and project operations.

### File Operations
- **`open_file(fname, flags, group)`** — Opens a named file. "Note that as file loading is asynchronous, operations on the returned view won't be possible until its `is_loading()` method returns `False`."
- **`new_file(flags, syntax)`** — Creates a new empty file with optional syntax specification
- **`find_open_file(fname, group)`** — Locates an already-open file by path, returning the View or None
- **`file_history()`** — Returns "the list of previously opened files. This is the same list as _File > Open Recent_."

### Sheet and View Management
- **`active_sheet()`** / **`active_view()`** — Returns the currently focused sheet or view
- **`sheets()`** / **`views()`** — Lists all open sheets or views in the window
- **`focus_sheet(sheet)`** / **`focus_view(view)`** — Switches to a specific sheet or view
- **`select_sheets(sheets)`** — Changes selected sheets for the entire window

### Group Organization
- **`num_groups()`** — Returns the number of view groups
- **`active_group()`** — Gets the currently selected group index
- **`focus_group(idx)`** — Focuses a specified group
- **`sheets_in_group(group)`** / **`views_in_group(group)`** — Lists sheets or views in a group
- **`set_sheet_index(sheet, group, index)`** — Moves a sheet to a specific group and position

### Symbol and Indexing
- **`symbol_locations(sym, source, type, kind_id, kind_letter)`** — "Find all locations where the symbol `sym` is located" with filtering options for source type and symbol kind
- **`lookup_symbol_in_index(symbol)`** — Returns "all locations where the symbol is defined across files in the current project" (deprecated in favor of `symbol_locations`)
- **`lookup_symbol_in_open_files(symbol)`** — Searches defined symbols in open files only (deprecated)
- **`lookup_references_in_index(symbol)`** — Finds symbol references across project files (deprecated)

### Project and Settings
- **`folders()`** — Returns "a list of the currently open folders"
- **`project_file_name()`** — Gets the current .sublime-project filename if available
- **`project_data()`** / **`set_project_data(data)`** — Accesses project metadata
- **`settings()`** — Returns window-specific Settings object
- **`extract_variables()`** — Provides contextual variables like file path, project name, and platform

### UI Control
- **`is_sidebar_visible()`** / **`set_sidebar_visible(flag)`** — Controls sidebar visibility
- **`is_minimap_visible()`** / **`set_minimap_visible(flag)`** — Manages minimap display
- **`set_menu_visible(flag)`** / **`set_tabs_visible(flag)`** — Shows or hides UI elements

### Dialogs and Input
- **`show_quick_panel(items, on_select, flags, selected_index, on_highlight, placeholder)`** — Displays "a quick panel to select an item in a list"
- **`show_input_panel(caption, initial_text, on_done, on_change, on_cancel)`** — Shows "the input panel, to collect a line of input from the user"
- **`create_output_panel(name, unlisted)`** — Finds or creates an output panel for displaying content

## Edit Class
An `Edit` object groups buffer modifications. "Edit objects are passed to TextCommands, and can not be created by the user."

## Region Class
Represents a text selection or buffer area with ordered endpoints:
- **`a`, `b`** — The region's endpoints; `b` may be before `a`
- **`begin()`** / **`end()`** — Returns smaller/larger endpoint
- **`empty()`** — Checks if region is zero-length
- **`cover(region)`** / **`intersection(region)`** — Combines or overlaps regions

## Settings Class
Manages configuration with key-value access and persistence across sessions.

## Key Type Definitions
- **`Point`** — Integer offset from buffer beginning
- **`Region`** — Pair of points representing a text span
- **`Value`** — JSON-compatible data (bool, str, int, float, list, dict, or None)
- **`CommandArgs`** — Optional dictionary of command parameters
- **`Kind`** — Tuple of (KindId, letter, description) for completion/symbol metadata

---

## Notes on Limitations

### No Batch File Listing API
Sublime does NOT provide an API to list all project files. Options:
- `window.folders()` - only returns folder roots
- `os.walk()` on folders - same as what Glob does
- No access to Sublime's internal file index

### Symbol Search Requires Query
- `symbol_locations(sym, ...)` - requires a symbol name to search
- `lookup_symbol_in_index(symbol)` - requires a symbol name
- No "list all symbols" or "search by pattern" API
