# io/tracker_io.py

"""
IO module for tracker file operations using contextual keys.
Handles reading, writing, merging and exporting tracker files.
"""

from collections import defaultdict
import datetime
import io
import json
import os
import re
import shutil
from typing import Dict, List, Tuple, Any, Optional, Set
from cline_utils.dependency_system.core.key_manager import (
    KeyInfo, # Added
    validate_key,
    sort_keys as sort_key_info, # Renamed for clarity - only use for List[KeyInfo]
    get_key_from_path as get_key_string_from_path, # Renamed for clarity
    sort_key_strings_hierarchically
)
from cline_utils.dependency_system.utils.path_utils import get_project_root, is_subpath, normalize_path, join_paths
from cline_utils.dependency_system.utils.config_manager import ConfigManager
from cline_utils.dependency_system.io.update_doc_tracker import doc_tracker_data
from cline_utils.dependency_system.io.update_mini_tracker import get_mini_tracker_data
from cline_utils.dependency_system.io.update_main_tracker import main_tracker_data
from cline_utils.dependency_system.utils.cache_manager import cached, check_file_modified, invalidate_dependent_entries
from cline_utils.dependency_system.core.dependency_grid import compress, create_initial_grid, decompress, validate_grid, PLACEHOLDER_CHAR, EMPTY_CHAR, DIAGONAL_CHAR

import logging

logger = logging.getLogger(__name__)

# --- REMOVE THIS Utility ---
# def _sort_key_strings(keys: List[str]) -> List[str]:
#     """Sorts a list of key strings using standard sorting."""
#     # If natural sorting (like sort_keys used to do for strings) is needed, implement here.
#     # For now, standard sort is used.
#     return sorted(keys) # <<< INCORRECT SORTING

# --- Path Finding ---
# Caching for get_tracker_path (consider config mtime)
# @cached("tracker_paths",
#         key_func=lambda project_root, tracker_type="main", module_path=None:
#         f"tracker_path:{normalize_path(project_root)}:{tracker_type}:{normalize_path(module_path) if module_path else 'none'}:{(os.path.getmtime(ConfigManager().config_path) if os.path.exists(ConfigManager().config_path) else 0)}")
def get_tracker_path(project_root: str, tracker_type: str = "main", module_path: Optional[str] = None) -> str:
    """
    Get the path to the appropriate tracker file based on type. Ensures path uses forward slashes.

    Args:
        project_root: Project root directory
        tracker_type: Type of tracker ('main', 'doc', or 'mini')
        module_path: The module path (required for mini-trackers)
    Returns:
        Normalized path to the tracker file using forward slashes
    """
    project_root = normalize_path(project_root)
    norm_module_path = normalize_path(module_path) if module_path else None

    if tracker_type == "main":
        return normalize_path(main_tracker_data["get_tracker_path"](project_root))
    elif tracker_type == "doc":
        return normalize_path(doc_tracker_data["get_tracker_path"](project_root))
    elif tracker_type == "mini":
        if not norm_module_path:
            raise ValueError("module_path must be provided for mini-trackers")
        # Use the dedicated function from the mini tracker data structure if available
        if "get_tracker_path" in get_mini_tracker_data():
             return normalize_path(get_mini_tracker_data()["get_tracker_path"](norm_module_path))
        else:
             # Fallback logic if get_tracker_path is not in mini_tracker_data
             module_name = os.path.basename(norm_module_path)
             raw_path = os.path.join(norm_module_path, f"{module_name}_module.md")
             return normalize_path(raw_path)
    else:
        raise ValueError(f"Unknown tracker type: {tracker_type}")

# --- File Reading ---
# Caching for read_tracker_file based on path and modification time.
# @cached("tracker_data",
#         key_func=lambda tracker_path:
#         f"tracker_data:{normalize_path(tracker_path)}:{(os.path.getmtime(tracker_path) if os.path.exists(tracker_path) else 0)}")
def read_tracker_file(tracker_path: str) -> Dict[str, Any]:
    """
    Read a tracker file and parse its contents. Caches based on path and mtime.
    Args:
        tracker_path: Path to the tracker file
    Returns:
        Dictionary with keys, grid, and metadata, or empty structure on failure.
    """
    tracker_path = normalize_path(tracker_path)
    if not os.path.exists(tracker_path):
        logger.debug(f"Tracker file not found: {tracker_path}. Returning empty structure.")
        return {"keys": {}, "grid": {}, "last_key_edit": "", "last_grid_edit": ""}
    try:
        with open(tracker_path, 'r', encoding='utf-8') as f: content = f.read()
        keys = {}; grid = {}; last_key_edit = ""; last_grid_edit = ""
        key_section_match = re.search(r'---KEY_DEFINITIONS_START---\n(.*?)\n---KEY_DEFINITIONS_END---', content, re.DOTALL | re.IGNORECASE)
        if key_section_match:
            key_section_content = key_section_match.group(1)
            for line in key_section_content.splitlines():
                line = line.strip()
                if not line or line.lower().startswith("key definitions:"): continue
                match = re.match(r'^([a-zA-Z0-9]+)\s*:\s*(.*)$', line)
                if match:
                    k, v = match.groups()
                    if validate_key(k): keys[k] = normalize_path(v.strip())
                    else: logger.warning(f"Skipping invalid key format in {tracker_path}: '{k}'")

        grid_section_match = re.search(r'---GRID_START---\n(.*?)\n---GRID_END---', content, re.DOTALL | re.IGNORECASE)
        if grid_section_match:
            grid_section_content = grid_section_match.group(1)
            lines = grid_section_content.strip().splitlines()
            # Skip header line (X ...) if present
            if lines and (lines[0].strip().upper().startswith("X ") or lines[0].strip() == "X"): lines = lines[1:]
            for line in lines:
                line = line.strip()
                match = re.match(r'^([a-zA-Z0-9]+)\s*=\s*(.*)$', line)
                if match:
                    k, v = match.groups()
                    if validate_key(k): grid[k] = v.strip()
                    else: logger.warning(f"Grid row key '{k}' in {tracker_path} has invalid format. Skipping.")

        last_key_edit_match = re.search(r'^last_KEY_edit\s*:\s*(.*)$', content, re.MULTILINE | re.IGNORECASE)
        if last_key_edit_match: last_key_edit = last_key_edit_match.group(1).strip()
        last_grid_edit_match = re.search(r'^last_GRID_edit\s*:\s*(.*)$', content, re.MULTILINE | re.IGNORECASE)
        if last_grid_edit_match: last_grid_edit = last_grid_edit_match.group(1).strip()

        logger.debug(f"Read tracker '{os.path.basename(tracker_path)}': {len(keys)} keys, {len(grid)} grid rows")
        return {"keys": keys, "grid": grid, "last_key_edit": last_key_edit, "last_grid_edit": last_grid_edit}
    except Exception as e:
        logger.exception(f"Error reading tracker file {tracker_path}: {e}")
        return {"keys": {}, "grid": {}, "last_key_edit": "", "last_grid_edit": ""}

# --- File Writing ---
def write_tracker_file(tracker_path: str,
                       key_defs_to_write: Dict[str, str], # Key string -> Path string map
                       grid_to_write: Dict[str, str], # Key string -> Compressed row map
                       last_key_edit: str,
                       last_grid_edit: str = "") -> bool:
    """
    Write tracker data to a file in markdown format. Ensures directory exists.
    Performs validation before writing. Uses standard sorting for key strings.

    Args:
        tracker_path: Path to the tracker file
        key_defs_to_write: Dictionary of keys strings to path strings for definitions.
        grid_to_write: Dictionary of grid rows (compressed strings), keyed by key strings.
        last_key_edit: Last key edit identifier
        last_grid_edit: Last grid edit identifier
    Returns:
        True if successful, False otherwise
    """
    tracker_path = normalize_path(tracker_path)
    try:
        dirname = os.path.dirname(tracker_path); os.makedirs(dirname, exist_ok=True)

        # <<< *** MODIFIED SORTING *** >>>
        # Sort key strings using standard sorting
        sorted_keys_list = sort_key_strings_hierarchically(list(key_defs_to_write.keys()))

        # --- Validate grid before writing ---
        if not validate_grid(grid_to_write, sorted_keys_list): # Validation uses key strings
            logger.error(f"Aborting write to {tracker_path} due to grid validation failure.")
            return False

        # Rebuild/Fix Grid to ensure consistency with sorted_keys_list
        final_grid = {}
        expected_len = len(sorted_keys_list)
        key_to_idx = {key: i for i, key in enumerate(sorted_keys_list)}
        for row_key in sorted_keys_list:
            compressed_row = grid_to_write.get(row_key); row_list = None
            if compressed_row is not None:
                try:
                    decompressed_row = decompress(compressed_row)
                    if len(decompressed_row) == expected_len: row_list = list(decompressed_row)
                    else: logger.warning(f"Correcting grid row length for key '{row_key}' in {tracker_path} (expected {expected_len}, got {len(decompressed_row)}).")
                except Exception as decomp_err: logger.warning(f"Error decompressing row for key '{row_key}' in {tracker_path}: {decomp_err}. Re-initializing.")
            if row_list is None:
                row_list = [PLACEHOLDER_CHAR] * expected_len
                row_idx = key_to_idx.get(row_key)
                if row_idx is not None: row_list[row_idx] = DIAGONAL_CHAR
                else: logger.error(f"Key '{row_key}' not found in index map during grid rebuild!")
            final_grid[row_key] = compress("".join(row_list))

        # --- Write Content ---
        with open(tracker_path, 'w', encoding='utf-8', newline='\n') as f:
            # Write key definitions using the provided map and sorted list
            f.write("---KEY_DEFINITIONS_START---\n"); f.write("Key Definitions:\n")
            for key in sorted_keys_list:
                f.write(f"{key}: {normalize_path(key_defs_to_write[key])}\n") # Ensure path uses forward slashes
            f.write("---KEY_DEFINITIONS_END---\n\n")

            # Write metadata
            f.write(f"last_KEY_edit: {last_key_edit}\n"); f.write(f"last_GRID_edit: {last_grid_edit}\n\n")

            # Write grid using the validated/rebuilt grid
            f.write("---GRID_START---\n")
            if sorted_keys_list:
                f.write(f"X {' '.join(sorted_keys_list)}\n")
                for key in sorted_keys_list:
                    f.write(f"{key} = {final_grid.get(key, '')}\n") # Use final_grid
            else: f.write("X \n")
            f.write("---GRID_END---\n")

        logger.info(f"Successfully wrote tracker file: {tracker_path} with {len(sorted_keys_list)} keys.")
        # Invalidate cache for this specific tracker file after writing
        invalidate_dependent_entries('tracker_data', f"tracker_data:{tracker_path}:.*")
        return True
    except IOError as e:
        logger.error(f"I/O Error writing tracker file {tracker_path}: {e}", exc_info=True); return False
    except Exception as e:
        logger.exception(f"Unexpected error writing tracker file {tracker_path}: {e}"); return False


# --- Backup ---
def backup_tracker_file(tracker_path: str) -> str:
    """
    Create a backup of a tracker file, keeping the 2 most recent backups.

    Args:
        tracker_path: Path to the tracker file
    Returns:
        Path to the backup file or empty string on failure
    """
    tracker_path = normalize_path(tracker_path)
    if not os.path.exists(tracker_path): logger.warning(f"Tracker file not found for backup: {tracker_path}"); return ""
    try:
        config = ConfigManager(); project_root = get_project_root()
        backup_dir_rel = config.get_path("backups_dir", "cline_docs/backups")
        backup_dir_abs = normalize_path(os.path.join(project_root, backup_dir_rel))
        os.makedirs(backup_dir_abs, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        base_name = os.path.basename(tracker_path)
        backup_filename = f"{base_name}.{timestamp}.bak"
        backup_path = os.path.join(backup_dir_abs, backup_filename)
        shutil.copy2(tracker_path, backup_path)
        logger.info(f"Backed up tracker '{base_name}' to: {os.path.basename(backup_path)}")
        # --- Cleanup old backups ---
        try:
            # Find all backups for this specific base name
            backup_files = []
            for filename in os.listdir(backup_dir_abs):
                if filename.startswith(base_name + ".") and filename.endswith(".bak"):
                    # Extract timestamp (handle potential variations if needed)
                    # Assuming format base_name.YYYYMMDD_HHMMSS_ffffff.bak
                    match = re.search(r'\.(\d{8}_\d{6}_\d{6})\.bak$', filename)
                    if match:
                        timestamp_str = match.group(1)
                        try:
                            # Use timestamp object for reliable sorting
                            file_timestamp = datetime.datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S_%f")
                            backup_files.append((file_timestamp, os.path.join(backup_dir_abs, filename)))
                        except ValueError: logger.warning(f"Could not parse timestamp for backup: {filename}")
            backup_files.sort(key=lambda x: x[0], reverse=True)
            if len(backup_files) > 2:
                files_to_delete = backup_files[2:]
                logger.debug(f"Cleaning up {len(files_to_delete)} older backups for '{base_name}'.")
                for _, file_path_to_delete in files_to_delete:
                    try: os.remove(file_path_to_delete)
                    except OSError as delete_error: logger.error(f"Error deleting old backup {file_path_to_delete}: {delete_error}")
        except Exception as cleanup_error: logger.error(f"Error during backup cleanup for {base_name}: {cleanup_error}")
        return backup_path
    except Exception as e:
        logger.error(f"Error backing up tracker file {tracker_path}: {e}", exc_info=True); return ""

# --- Merge Helpers ---
# _merge_grids: Replace sort_keys with sort_key_strings_hierarchically
def _merge_grids(primary_grid: Dict[str, str], secondary_grid: Dict[str, str],
                 primary_keys_list: List[str], secondary_keys_list: List[str],
                 merged_keys_list: List[str]) -> Dict[str, str]:
    """Merges two decompressed grids based on the merged key list. Primary overwrites secondary."""
    merged_decompressed_grid = {}; merged_size = len(merged_keys_list)
    key_to_merged_idx = {key: i for i, key in enumerate(merged_keys_list)}
    # Initialize merged grid with placeholders and diagonal
    for i, row_key in enumerate(merged_keys_list):
        row = [PLACEHOLDER_CHAR] * merged_size; row[i] = DIAGONAL_CHAR
        merged_decompressed_grid[row_key] = row
    config = ConfigManager(); get_priority = config.get_char_priority
    # Decompress input grids (handle potential errors)
    def safe_decompress(grid_data, keys_list):
        decomp_grid = {}; key_to_idx = {k: i for i, k in enumerate(keys_list)}; expected_len = len(keys_list)
        for key, compressed in grid_data.items():
            if key not in key_to_idx: continue
            try:
                decomp = list(decompress(compressed))
                if len(decomp) == expected_len: decomp_grid[key] = decomp
                else: logger.warning(f"Merge Prep: Incorrect length for key '{key}' (expected {expected_len}, got {len(decomp)}). Skipping row.")
            except Exception as e: logger.warning(f"Merge Prep: Failed to decompress row for key '{key}': {e}. Skipping row.")
        return decomp_grid
    primary_decomp = safe_decompress(primary_grid, primary_keys_list)
    secondary_decomp = safe_decompress(secondary_grid, secondary_keys_list)
    key_to_primary_idx = {key: i for i, key in enumerate(primary_keys_list)}
    key_to_secondary_idx = {key: i for i, key in enumerate(secondary_keys_list)}
    # Apply values based on merged keys
    for row_key in merged_keys_list:
        merged_row_idx = key_to_merged_idx[row_key]
        for col_key in merged_keys_list:
            merged_col_idx = key_to_merged_idx[col_key]
            if merged_row_idx == merged_col_idx: continue # Skip diagonal
            # Get values from original grids if they exist
            primary_val = None
            if row_key in primary_decomp and col_key in key_to_primary_idx:
                 pri_col_idx = key_to_primary_idx[col_key]
                 if pri_col_idx < len(primary_decomp[row_key]): primary_val = primary_decomp[row_key][pri_col_idx]
            secondary_val = None
            if row_key in secondary_decomp and col_key in key_to_secondary_idx:
                 sec_col_idx = key_to_secondary_idx[col_key]
                 if sec_col_idx < len(secondary_decomp[row_key]): secondary_val = secondary_decomp[row_key][sec_col_idx]
            # Determine final value (primary takes precedence over secondary, ignore placeholders)
            final_val = PLACEHOLDER_CHAR
            if primary_val is not None and primary_val != PLACEHOLDER_CHAR: final_val = primary_val
            elif secondary_val is not None and secondary_val != PLACEHOLDER_CHAR: final_val = secondary_val
            merged_decompressed_grid[row_key][merged_col_idx] = final_val
    compressed_grid = {key: compress("".join(row_list)) for key, row_list in merged_decompressed_grid.items()}
    return compressed_grid

# merge_trackers: Replace sort_keys with sort_key_strings_hierarchically
def merge_trackers(primary_tracker_path: str, secondary_tracker_path: str, output_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Merge two tracker files, with the primary taking precedence. Invalidates relevant caches.

    Args:
        primary_tracker_path: Path to the primary tracker file
        secondary_tracker_path: Path to the secondary tracker file
        output_path: Path to write the merged tracker. If None, overwrites primary.
    Returns:
        Merged tracker data as a dictionary, or None on failure.
    """
    primary_tracker_path = normalize_path(primary_tracker_path)
    secondary_tracker_path = normalize_path(secondary_tracker_path)
    output_path = normalize_path(output_path) if output_path else primary_tracker_path
    logger.info(f"Attempting to merge '{os.path.basename(primary_tracker_path)}' and '{os.path.basename(secondary_tracker_path)}' into '{os.path.basename(output_path)}'")
    # Backup before potentially overwriting
    backup_made = False
    if output_path == primary_tracker_path and os.path.exists(primary_tracker_path): backup_tracker_file(primary_tracker_path); backup_made = True
    elif output_path == secondary_tracker_path and os.path.exists(secondary_tracker_path): backup_tracker_file(secondary_tracker_path); backup_made = True
    if backup_made: logger.info(f"Backed up target file before merge: {os.path.basename(output_path)}")
    # Read both trackers (using cached read)
    primary_data = read_tracker_file(primary_tracker_path); secondary_data = read_tracker_file(secondary_tracker_path)
    # Check if data is valid
    primary_keys = primary_data.get("keys", {}); secondary_keys = secondary_data.get("keys", {})
    if not primary_keys and not secondary_keys: logger.warning("Both trackers are empty or unreadable. Cannot merge."); return None
    elif not primary_keys: logger.info(f"Primary tracker {os.path.basename(primary_tracker_path)} empty/unreadable. Using secondary tracker."); merged_data = secondary_data
    elif not secondary_keys: logger.info(f"Secondary tracker {os.path.basename(secondary_tracker_path)} empty/unreadable. Using primary tracker."); merged_data = primary_data
    else:
        logger.debug(f"Merging {len(primary_keys)} primary keys and {len(secondary_keys)} secondary keys.")
        # Merge keys (primary takes precedence for path if key exists in both)
        merged_keys_map = {**secondary_keys, **primary_keys}
        # <<< *** Use HIERARCHICAL SORT DIRECTLY *** >>>
        merged_keys_list = sort_key_strings_hierarchically(list(merged_keys_map.keys()))
        merged_compressed_grid = _merge_grids(
            primary_data.get("grid", {}), secondary_data.get("grid", {}),
            sort_key_strings_hierarchically(list(primary_keys.keys())),
            sort_key_strings_hierarchically(list(secondary_keys.keys())),
            merged_keys_list
        )
        # Merge metadata (simple precedence for now, consider timestamp comparison?)
        merged_last_key_edit = primary_data.get("last_key_edit", "") or secondary_data.get("last_key_edit", "")
        # Use a timestamp for the merge event itself?
        merged_last_grid_edit = f"Merged from {os.path.basename(primary_tracker_path)} and {os.path.basename(secondary_tracker_path)} on {datetime.datetime.now().isoformat()}"
        merged_data = {
            "keys": merged_keys_map, "grid": merged_compressed_grid,
            "last_key_edit": merged_last_key_edit, "last_grid_edit": merged_last_grid_edit,
        }
    # Write the merged tracker
    if write_tracker_file(output_path, merged_data["keys"], merged_data["grid"], merged_data["last_key_edit"], merged_data["last_grid_edit"]):
        logger.info(f"Successfully merged trackers into: {output_path}")
        # Invalidate caches related to the output file AND potentially source files if output overwrites
        invalidate_dependent_entries('tracker_data', f"tracker_data:{output_path}:.*")
        if output_path == primary_tracker_path: invalidate_dependent_entries('tracker_data', f"tracker_data:{primary_tracker_path}:.*")
        if output_path == secondary_tracker_path: invalidate_dependent_entries('tracker_data', f"tracker_data:{secondary_tracker_path}:.*")
        invalidate_dependent_entries('grid_decompress', '.*'); invalidate_dependent_entries('grid_validation', '.*'); invalidate_dependent_entries('grid_dependencies', '.*')
        return merged_data
    else:
        logger.error(f"Failed to write merged tracker to: {output_path}"); return None

# --- Read/Write Helpers ---
def _read_existing_keys(lines: List[str]) -> Dict[str, str]:
    """Reads existing key definitions from lines."""
    key_map = {}; in_section = False; key_def_start_pattern = re.compile(r'^---KEY_DEFINITIONS_START---$', re.IGNORECASE); key_def_end_pattern = re.compile(r'^---KEY_DEFINITIONS_END---$', re.IGNORECASE)
    for line in lines:
        if key_def_end_pattern.match(line.strip()): # Check stripped line for end marker
            break # Stop processing after end marker
        if in_section:
            line_content = line.strip()
            if not line_content or line_content.lower().startswith("key definitions:"): continue
            match = re.match(r'^([a-zA-Z0-9]+)\s*:\s*(.*)$', line_content)
            if match:
                k, v = match.groups()
                if validate_key(k): key_map[k] = normalize_path(v.strip())
        elif key_def_start_pattern.match(line.strip()): in_section = True
    return key_map

def _read_existing_grid(lines: List[str]) -> Dict[str, str]:
    """Reads the existing compressed grid data from lines."""
    grid_map = {}; in_section = False; grid_start_pattern = re.compile(r'^---GRID_START---$', re.IGNORECASE); grid_end_pattern = re.compile(r'^---GRID_END---$', re.IGNORECASE)
    for line in lines:
        if grid_end_pattern.match(line.strip()): break
        if in_section:
            line_content = line.strip()
            if line_content.upper().startswith("X ") or line_content == "X": continue
            match = re.match(r'^([a-zA-Z0-9]+)\s*=\s*(.*)$', line_content)
            if match:
                k, v = match.groups()
                if validate_key(k): grid_map[k] = v.strip()
        elif grid_start_pattern.match(line.strip()): in_section = True
    return grid_map

# _write_key_definitions, _write_grid: Replace sort_keys with sort_key_strings_hierarchically
def _write_key_definitions(file_obj: io.TextIOBase, key_map: Dict[str, str], sorted_keys_list: List[str]):
    """Writes the key definitions section using the pre-sorted list."""
    # <<< *** REMOVED INTERNAL SORT - Relies on passed list *** >>>
    file_obj.write("---KEY_DEFINITIONS_START---\n"); file_obj.write("Key Definitions:\n")
    for k in sorted_keys_list: # Iterate pre-sorted list
        v = key_map.get(k, "PATH_ERROR")
        if v != "PATH_ERROR": file_obj.write(f"{k}: {normalize_path(v)}\n")
        else: logger.error(...)
    file_obj.write("---KEY_DEFINITIONS_END---\n")

def _write_grid(file_obj: io.TextIOBase, sorted_keys_list: List[str], grid: Dict[str, str]):
    """Writes the grid section to the provided file object, ensuring correctness."""
    file_obj.write("---GRID_START---\n")
    if not sorted_keys_list: file_obj.write("X \n")
    else:
        file_obj.write(f"X {' '.join(sorted_keys_list)}\n")
        expected_len = len(sorted_keys_list); key_to_idx = {key: i for i, key in enumerate(sorted_keys_list)}
        for row_key in sorted_keys_list:
            compressed_row = grid.get(row_key); final_compressed_row = None
            if compressed_row is not None:
                try:
                    decompressed = decompress(compressed_row)
                    if len(decompressed) == expected_len: final_compressed_row = compressed_row
                    else: logger.warning(f"Correcting grid row length for key '{row_key}' before write...")
                except Exception: logger.warning(f"Error decompressing row for key '{row_key}' before write...")
            if final_compressed_row is None:
                 row_list = [PLACEHOLDER_CHAR] * expected_len
                 row_idx = key_to_idx.get(row_key)
                 if row_idx is not None: row_list[row_idx] = DIAGONAL_CHAR
                 final_compressed_row = compress("".join(row_list))
            file_obj.write(f"{row_key} = {final_compressed_row}\n")
    file_obj.write("---GRID_END---\n")

# --- Mini Tracker Specific Functions ---
def get_mini_tracker_path(module_path: str) -> str:
    """Gets the path to the mini tracker file using the function from mini_tracker_data."""
    norm_module_path = normalize_path(module_path)
    mini_data = get_mini_tracker_data()
    if "get_tracker_path" in mini_data:
        return normalize_path(mini_data["get_tracker_path"](norm_module_path))
    else:
        # Fallback if function is missing
        module_name = os.path.basename(norm_module_path)
        raw_path = os.path.join(norm_module_path, f"{module_name}_module.md")
        return normalize_path(raw_path)

# create_mini_tracker: Adapt to use path_to_key_info
def create_mini_tracker(module_path: str,
                        path_to_key_info: Dict[str, KeyInfo], # <<< CHANGED
                        relevant_keys_for_grid: List[str], # Key strings needed in grid
                        new_key_strings_for_this_tracker: Optional[List[str]] = None): # Relevant NEW key STRINGS
    """Creates a new mini-tracker file with the template."""
    mini_tracker_info = get_mini_tracker_data()
    template = mini_tracker_info["template"]
    marker_start, marker_end = mini_tracker_info["markers"]
    norm_module_path = normalize_path(module_path)
    module_name = os.path.basename(norm_module_path)
    output_file = get_mini_tracker_path(norm_module_path) # Use helper

    # Definitions include keys relevant to the grid, get paths from global map
    # <<< *** MODIFIED LOGIC *** >>>
    keys_to_write_defs: Dict[str, str] = {}
    for k_str in relevant_keys_for_grid:
         # Find the KeyInfo object associated with this key string
         # This is inefficient - ideally caller provides KeyInfo for relevant keys
         # For now, search the global map (can be slow for large projects)
         found_info = next((info for info in path_to_key_info.values() if info.key_string == k_str), None)
         if found_info:
              keys_to_write_defs[k_str] = found_info.norm_path
         else:
              keys_to_write_defs[k_str] = "PATH_NOT_FOUND_IN_GLOBAL_MAP"
              logger.warning(f"Key string '{k_str}' needed for mini-tracker '{os.path.basename(output_file)}' definitions not found in global path_to_key_info.")

    # Ensure module's own key is included if it exists
    # <<< *** MODIFIED LOGIC *** >>>
    module_key_string = get_key_string_from_path(norm_module_path, path_to_key_info) # Use updated function name
    if module_key_string:
        if module_key_string not in relevant_keys_for_grid:
            relevant_keys_for_grid.append(module_key_string) # Add if missing
        if module_key_string not in keys_to_write_defs:
             keys_to_write_defs[module_key_string] = norm_module_path

    # Grid dimensions are based on relevant_keys_for_grid
    # <<< *** ASSUME relevant_keys_for_grid IS ALREADY SORTED HIERARCHICALLY *** >>>
    sorted_relevant_keys_list = relevant_keys_for_grid # Use directly
    try:
        dirname = os.path.dirname(output_file); os.makedirs(dirname, exist_ok=True)
        with open(output_file, "w", encoding="utf-8", newline='\n') as f:
            try: f.write(template.format(module_name=module_name))
            except KeyError: f.write(template)
            if marker_start not in template: f.write("\n" + marker_start + "\n")
            f.write("\n")
            # --- Write the tracker data section ---
            _write_key_definitions(f, keys_to_write_defs, sorted_relevant_keys_list)
            f.write("\n")
            # <<< *** MODIFIED metadata message *** >>>
            last_key_edit_msg = f"Assigned keys: {', '.join(new_key_strings_for_this_tracker)}" if new_key_strings_for_this_tracker else (f"Initial key: {module_key_string}" if module_key_string else "Initial creation")
            f.write(f"last_KEY_edit: {last_key_edit_msg}\n")
            f.write(f"last_GRID_edit: Initial creation\n\n")
            # Write the grid using the relevant keys and an initial empty grid
            initial_grid = create_initial_grid(sorted_relevant_keys_list)
            _write_grid(f, sorted_relevant_keys_list, initial_grid)
            f.write("\n")
            if marker_end not in template: f.write(marker_end + "\n")
        logger.info(f"Created new mini tracker: {output_file}")
        return True
    except IOError as e: logger.error(f"I/O Error creating mini tracker {output_file}: {e}", exc_info=True); return False
    except Exception as e: logger.exception(f"Unexpected error creating mini tracker {output_file}: {e}"); return False

# --- update_tracker (Main dispatcher) ---
def update_tracker(output_file_suggestion: str, # Path suggestion (may be ignored for mini/main)
                   path_to_key_info: Dict[str, KeyInfo], # GLOBAL path -> KeyInfo map
                   tracker_type: str = "main",
                   suggestions: Optional[Dict[str, List[Tuple[str, str]]]] = None, # Key STRINGS -> (Key STRING, char)
                   file_to_module: Optional[Dict[str, str]] = None, # norm_file_path -> norm_module_path
                   new_keys: Optional[List[KeyInfo]] = None): # GLOBAL list of new KeyInfo objects
    """
    Updates or creates a tracker file based on type using contextual keys.
    Invalidates cache on changes.
    Calls tracker-specific logic for filtering, aggregation (main), and path determination.
    Uses hierarchical sorting for key strings.
    """
    project_root = get_project_root()
    config = ConfigManager()
    get_priority = config.get_char_priority

    output_file = "" # Final path will be determined based on type
    # Keys relevant for DEFINITIONS in this tracker (Key String -> Path String)
    final_key_defs: Dict[str, str] = {}
    # Key STRINGS relevant for GRID rows/columns in this tracker
    relevant_keys_for_grid: List[str] = []
    # Suggestions filtered/aggregated for THIS tracker (Key STRING -> List[(Key STRING, char)])
    final_suggestions_to_apply = defaultdict(list)
    module_path = "" # Keep track of module path for mini-trackers

    # --- Determine Type-Specific Settings ---
    if tracker_type == "main":
        output_file = main_tracker_data["get_tracker_path"](project_root)
        # Filter returns Dict[norm_path, KeyInfo]
        filtered_modules_info = main_tracker_data["key_filter"](project_root, path_to_key_info)
        # Extract key strings for the grid
        relevant_keys_for_grid = [info.key_string for info in filtered_modules_info.values()]
        # Extract definitions (Key String -> Path String)
        final_key_defs = {info.key_string: info.norm_path for info in filtered_modules_info.values()}

        logger.info(f"Main tracker update for {len(relevant_keys_for_grid)} modules.")
        logger.debug("Performing main tracker aggregation...")
        try:
            # Aggregation uses path_to_key_info and filtered_modules_info (path->info)
            # Result is Dict[Source Path -> List[(Target Path, char)]]
            aggregated_result_paths = main_tracker_data["dependency_aggregation"](
                project_root, path_to_key_info, filtered_modules_info, file_to_module
            )
            # Convert path-based aggregation result to key-string based suggestions
            logger.debug("Converting aggregated path results to key string suggestions...")
            for src_path, targets in aggregated_result_paths.items():
                 src_key_info = path_to_key_info.get(src_path)
                 if not src_key_info: logger.warning(f"Agg-Convert: Source path {src_path} not found."); continue
                 src_key_string = src_key_info.key_string
                 if src_key_string not in final_key_defs: continue # Ensure source is in this grid

                 for target_path, dep_char in targets:
                      target_key_info = path_to_key_info.get(target_path)
                      if not target_key_info: logger.warning(f"Agg-Convert: Target path {target_path} not found."); continue
                      target_key_string = target_key_info.key_string
                      if target_key_string in final_key_defs: # Ensure target is in this grid
                           final_suggestions_to_apply[src_key_string].append((target_key_string, dep_char))
            logger.info(f"Main tracker aggregation complete. Found {sum(len(v) for v in final_suggestions_to_apply.values())} relevant aggregated dependencies.")
        except Exception as agg_err:
            logger.error(f"Main tracker aggregation failed: {agg_err}", exc_info=True)
            # Continue with empty suggestions if aggregation fails

    elif tracker_type == "doc":
        output_file = doc_tracker_data["get_tracker_path"](project_root)
        # Filter returns Dict[norm_path, KeyInfo] for items under doc roots
        filtered_doc_info = doc_tracker_data["file_inclusion"](project_root, path_to_key_info)
        # Definitions include ALL filtered items (files and directories)
        final_key_defs = {info.key_string: info.norm_path for info in filtered_doc_info.values()}
        # Grid keys MUST match the definition keys
        relevant_keys_for_grid = list(final_key_defs.keys()) # Use all keys from definitions

        logger.info(f"Doc tracker update. Definitions: {len(final_key_defs)} items. Grid keys: {len(relevant_keys_for_grid)}.")
        if suggestions:
             for src_key, targets in suggestions.items():
                  # Only include suggestions where source and target are in the final defs
                  if src_key in final_key_defs:
                       filtered_targets = [(tgt, c) for tgt, c in targets if tgt in final_key_defs]
                       if filtered_targets: final_suggestions_to_apply[src_key].extend(filtered_targets)

    elif tracker_type == "mini":
        # --- Mini Tracker Specific Logic ---
        if not file_to_module: logger.error("file_to_module mapping required for mini-tracker updates."); return
        if not path_to_key_info: logger.warning("Global path_to_key_info is empty."); return

        # Determine module path from the output file suggestion
        potential_module_path = os.path.dirname(normalize_path(output_file_suggestion))
        # Find the KeyInfo for this directory path
        module_key_info = path_to_key_info.get(potential_module_path)
        if not module_key_info or not module_key_info.is_directory:
             potential_module_path = os.path.dirname(potential_module_path)
             module_key_info = path_to_key_info.get(potential_module_path)
             if not module_key_info or not module_key_info.is_directory:
                  logger.error(f"Cannot determine valid module path/key from suggestion path: {output_file_suggestion} -> {potential_module_path}")
                  return

        module_path = potential_module_path
        module_key_string = module_key_info.key_string
        output_file = get_mini_tracker_path(module_path)

        # Filter KeyInfo for items internal to this module (parent path matches module path OR item is module dir)
        internal_keys_info: Dict[str, KeyInfo] = {
            p: info for p, info in path_to_key_info.items()
            if info.parent_path == module_path or p == module_path
        }
        internal_keys_set = {info.key_string for info in internal_keys_info.values()}
        # Definitions include only internal keys/paths for writing later
        final_key_defs_internal = {info.key_string: info.norm_path for info in internal_keys_info.values()}

        # Determine relevant keys for the grid (internal + external dependencies touched by non-excluded internal files)
        relevant_keys_strings_set = internal_keys_set.copy()

        # Get exclusion info
        config = ConfigManager()
        project_root_for_exclude = get_project_root()
        excluded_dirs_abs = {normalize_path(os.path.join(project_root_for_exclude, p)) for p in config.get_excluded_dirs()}
        excluded_files_abs = set(config.get_excluded_paths())
        all_excluded_abs = excluded_dirs_abs.union(excluded_files_abs)
        abs_doc_roots: Set[str] = {normalize_path(os.path.join(project_root, p)) for p in config.get_doc_directories()}

        # Identify relevant external keys based on suggestions
        raw_suggestions = suggestions if suggestions else {} # Use input suggestions
        if raw_suggestions:
            for src_key_str, deps in raw_suggestions.items():
                 source_is_internal = src_key_str in internal_keys_set
                 if source_is_internal:
                      src_path = final_key_defs_internal.get(src_key_str)
                      if src_path and src_path in all_excluded_abs: continue # Skip excluded source
                      relevant_keys_strings_set.add(src_key_str) # Add internal source to grid keys

                      for target_key_str, dep_char in deps:
                           if dep_char != PLACEHOLDER_CHAR and dep_char != DIAGONAL_CHAR:
                               target_info = next((info for info in path_to_key_info.values() if info.key_string == target_key_str), None)
                               if target_info and target_info.norm_path not in all_excluded_abs: # Check if target exists and not excluded
                                    relevant_keys_strings_set.add(target_key_str) # Add non-excluded target

            # Consider incoming dependencies to non-excluded internal files
            all_target_keys_in_suggestions = {tgt for deps in raw_suggestions.values() for tgt, _ in deps}
            for target_key_str in all_target_keys_in_suggestions:
                 if target_key_str in internal_keys_set:
                     target_path = final_key_defs_internal.get(target_key_str)
                     if target_path and target_path in all_excluded_abs: continue
                     for src_key_str, deps in raw_suggestions.items():
                          if any(t == target_key_str and c != PLACEHOLDER_CHAR and c != DIAGONAL_CHAR for t, c in deps):
                              source_info = next((info for info in path_to_key_info.values() if info.key_string == src_key_str), None)
                              if source_info and source_info.norm_path not in all_excluded_abs:
                                   relevant_keys_strings_set.add(src_key_str)

        # Sort the final set of key strings for the grid hierarchically
        relevant_keys_for_grid = sort_key_strings_hierarchically(list(relevant_keys_strings_set))
        # Final definitions map for writing: includes internal paths AND paths for relevant foreign keys
        final_key_defs = {}
        for k_str in relevant_keys_for_grid:
            info = next((info for info in path_to_key_info.values() if info.key_string == k_str), None)
            if info:
                 final_key_defs[k_str] = info.norm_path
            else:
                 # This case should be rare if relevant_keys were derived correctly
                 final_key_defs[k_str] = "PATH_NOT_FOUND_GLOBALLY"
                 logger.warning(f"Mini update: Path not found globally for relevant grid key {k_str}")

        logger.info(f"Mini tracker update for module {module_key_string} ({os.path.basename(module_path)}). Grid keys: {len(relevant_keys_for_grid)}.")

        # Filter suggestions for FOREIGN dependencies (Source internal -> Target external OR doc)
        filtered_suggestions_to_apply = defaultdict(list)
        if raw_suggestions and file_to_module:
             logger.debug(f"Filtering mini-tracker suggestions for foreign dependencies ({os.path.basename(output_file)})...")
             for src_key_str, deps in raw_suggestions.items():
                  if src_key_str not in internal_keys_set: continue

                  src_path = final_key_defs_internal.get(src_key_str) # Path comes from internal map
                  if not src_path or src_path in all_excluded_abs: continue

                  # Find source module path (could be file or the module dir itself)
                  src_module_path = file_to_module.get(src_path)
                  if not src_module_path and src_path == module_path: src_module_path = module_path
                  if not src_module_path or src_module_path != module_path: continue

                  for target_key_str, dep_char in deps:
                       if target_key_str not in relevant_keys_for_grid: continue

                       # Find path/info for target key string from GLOBAL map
                       target_info = next((info for info in path_to_key_info.values() if info.key_string == target_key_str), None)
                       if not target_info:
                            logger.warning(f"Mini foreign check: No path info for target key {target_key_str}. Assuming external.")
                            filtered_suggestions_to_apply[src_key_str].append((target_key_str, dep_char)); continue

                       target_path = target_info.norm_path
                       # Check if target is excluded
                       if target_path in all_excluded_abs: continue

                       is_foreign = False
                       is_target_in_doc_root = any(target_path == doc_root or is_subpath(target_path, doc_root) for doc_root in abs_doc_roots)

                       if is_target_in_doc_root:
                            is_foreign = True
                            # logger.debug(f"Mini foreign check: Target {target_key_str} is doc. Marking foreign.")
                       else:
                            target_module_path = file_to_module.get(target_path)
                            if not target_module_path and target_info.is_directory: target_module_path = target_path
                            if target_module_path and target_module_path != src_module_path: is_foreign = True
                            elif not target_module_path: is_foreign = True # Unknown module, assume foreign

                       if is_foreign: filtered_suggestions_to_apply[src_key_str].append((target_key_str, dep_char))

        # Override final_suggestions_to_apply for mini-trackers
        final_suggestions_to_apply = filtered_suggestions_to_apply
    else:
        raise ValueError(f"Unknown tracker type: {tracker_type}")

    # --- Common Logic: Read Existing / Create New ---
    check_file_modified(output_file) # Check cache validity
    keys_in_final_defs_set = set(final_key_defs.keys()) # Use final keys selected for this tracker
    relevant_new_keys_list = []
    if new_keys: # new_keys is List[KeyInfo]
        # Sort the NEW keys relevant to THIS tracker hierarchically
        relevant_new_keys_list = sort_key_strings_hierarchically([
            k_info.key_string for k_info in new_keys if k_info.key_string in keys_in_final_defs_set
        ])

    existing_key_defs = {}; existing_grid = {}; current_last_key_edit = ""; current_last_grid_edit = ""; lines = []
    tracker_exists = os.path.exists(output_file)
    if tracker_exists:
        try:
            with open(output_file, "r", encoding="utf-8") as f: lines = f.readlines()
            existing_key_defs = _read_existing_keys(lines); existing_grid = _read_existing_grid(lines)
            last_key_edit_line = next((l for l in lines if l.strip().lower().startswith("last_key_edit")), None)
            last_grid_edit_line = next((l for l in lines if l.strip().lower().startswith("last_grid_edit")), None)
            current_last_key_edit = last_key_edit_line.split(":", 1)[1].strip() if last_key_edit_line else "Unknown"
            current_last_grid_edit = last_grid_edit_line.split(":", 1)[1].strip() if last_grid_edit_line else "Unknown"
        except Exception as e:
            logger.error(f"Failed read existing tracker {output_file}: {e}. Cautious.", exc_info=True)
            existing_key_defs={}; existing_grid={}; current_last_key_edit=""; current_last_grid_edit=""; lines=[]; tracker_exists=False

    # Create tracker if it doesn't exist
    if not tracker_exists:
        logger.info(f"Tracker file not found: {output_file}. Creating new file.")
        created_ok = False
        # Ensure keys for creation are sorted hierarchically
        sorted_keys_list_for_create = sort_key_strings_hierarchically(relevant_keys_for_grid)

        if tracker_type == "mini":
            # Pass hierarchically sorted list for grid
            created_ok = create_mini_tracker(module_path, path_to_key_info, sorted_keys_list_for_create, relevant_new_keys_list)
        else: # Create main or doc tracker
            last_key_edit_msg = f"Assigned keys: {', '.join(relevant_new_keys_list)}" if relevant_new_keys_list else (f"Initial keys: {len(sorted_keys_list_for_create)}" if sorted_keys_list_for_create else "Initial creation")
            initial_grid = create_initial_grid(sorted_keys_list_for_create) # Use sorted list
            # Pass final_key_defs (map) and the sorted list to write_tracker_file
            created_ok = write_tracker_file(output_file, final_key_defs, initial_grid, last_key_edit_msg, "Initial creation")

        if not created_ok: logger.error(f"Failed to create new tracker {output_file}. Aborting update."); return
        try: # Re-read newly created file
            with open(output_file, "r", encoding="utf-8") as f: lines = f.readlines()
            existing_key_defs = _read_existing_keys(lines); existing_grid = _read_existing_grid(lines)
            current_last_key_edit = f"Assigned keys: {', '.join(relevant_new_keys_list)}" if relevant_new_keys_list else "Initial creation"
            current_last_grid_edit = "Initial creation"
        except Exception as e: logger.error(f"Failed to read newly created tracker {output_file}: {e}. Aborting update.", exc_info=True); return

    # --- Update Existing Tracker ---
    logger.debug(f"Updating tracker: {output_file}")
    if tracker_exists: backup_tracker_file(output_file)

    # --- Key Definition Update & Sorting ---
    # Use hierarchical sort for the final list of keys determined for this tracker
    final_sorted_keys_list = sort_key_strings_hierarchically(list(final_key_defs.keys()))

    # --- Determine Key Changes for Metadata ---
    existing_keys_in_file_set = set(existing_key_defs.keys()); keys_in_final_grid_set = set(final_sorted_keys_list)
    added_keys = keys_in_final_grid_set - existing_keys_in_file_set; removed_keys = existing_keys_in_file_set - keys_in_final_grid_set
    final_last_key_edit = current_last_key_edit
    if relevant_new_keys_list: final_last_key_edit = f"Assigned keys: {', '.join(relevant_new_keys_list)}"
    elif added_keys or removed_keys:
         change_parts = [];
         if added_keys: change_parts.append(f"Added {len(added_keys)} keys")
         if removed_keys: change_parts.append(f"Removed {len(removed_keys)} keys")
         final_last_key_edit = f"Keys updated: {'; '.join(change_parts)}"

    # --- Grid Structure Update ---
    final_grid = {}; grid_structure_changed = bool(added_keys or removed_keys); final_last_grid_edit = current_last_grid_edit
    if grid_structure_changed: final_last_grid_edit = f"Grid structure updated ({datetime.datetime.now().isoformat()})"
    temp_decomp_grid = {}
    # Use hierarchical sort for the list of keys that were in the old file
    old_keys_list = sort_key_strings_hierarchically(list(existing_key_defs.keys()))
    old_key_to_idx = {k: i for i, k in enumerate(old_keys_list)}; final_key_to_idx = {k: i for i, k in enumerate(final_sorted_keys_list)}
    # Initialize new grid structure based on the FINAL sorted list
    for row_key in final_sorted_keys_list:
        row_list = [PLACEHOLDER_CHAR] * len(final_sorted_keys_list); row_idx = final_key_to_idx.get(row_key)
        if row_idx is not None: row_list[row_idx] = DIAGONAL_CHAR
        temp_decomp_grid[row_key] = row_list
    # Copy old values using the OLD sorted list for indexing into decompressed old rows
    for old_row_key, compressed_row in existing_grid.items():
        if old_row_key in final_key_to_idx: # Only process rows still present
            try:
                decomp_row = list(decompress(compressed_row))
                if len(decomp_row) == len(old_keys_list): # Check against OLD length
                    for old_col_idx, value in enumerate(decomp_row):
                         if old_col_idx < len(old_keys_list):
                             old_col_key = old_keys_list[old_col_idx]
                             # If the old column key is still in the final grid
                             if old_col_key in final_key_to_idx:
                                 new_col_idx = final_key_to_idx[old_col_key]; new_row_idx = final_key_to_idx[old_row_key]
                                 if new_row_idx != new_col_idx: temp_decomp_grid[old_row_key][new_col_idx] = value
                else: logger.warning(f"Grid Rebuild: Row length mismatch for '{old_row_key}' in {output_file} (Expected: {len(old_keys_list)}, Got: {len(decomp_row)}). Skipping.")
            except Exception as e: logger.warning(f"Grid Rebuild: Error processing row '{old_row_key}' in {output_file}: {e}. Skipping.")

    # --- Apply Suggestions (Filtered or Aggregated) ---
    suggestion_applied = False
    if final_suggestions_to_apply: # Uses the correctly determined suggestions for this tracker type
        logger.debug(f"Applying {sum(len(v) for v in final_suggestions_to_apply.values())} suggestions to grid for {output_file}")
        for row_key, deps in final_suggestions_to_apply.items():
            if row_key not in final_key_to_idx: continue
            current_decomp_row = temp_decomp_grid.get(row_key)
            if not current_decomp_row: continue
            for col_key, dep_char in deps:
                if col_key not in final_key_to_idx: continue
                if row_key == col_key: continue
                col_idx = final_key_to_idx[col_key]; existing_char = current_decomp_row[col_idx]
                if existing_char == PLACEHOLDER_CHAR and dep_char != PLACEHOLDER_CHAR:
                    current_decomp_row[col_idx] = dep_char
                    if not suggestion_applied: final_last_grid_edit = f"Applied suggestions ({datetime.datetime.now().isoformat()})"
                    suggestion_applied = True
                    # logger.debug(f"Applied suggestion: {row_key} -> {col_key} ({dep_char}) in {output_file}")
                elif existing_char != PLACEHOLDER_CHAR and existing_char != DIAGONAL_CHAR and existing_char != dep_char:
                     warning_msg = (f"Suggestion Conflict in {os.path.basename(output_file)}: For {row_key}->{col_key}, grid has '{existing_char}', suggestion is '{dep_char}'. Grid value kept.")
                     logger.warning(warning_msg); print(f"WARNING: {warning_msg}") # Reduce console noise
            temp_decomp_grid[row_key] = current_decomp_row

    # Compress the final grid state
    final_grid = {key: compress("".join(row_list)) for key, row_list in temp_decomp_grid.items()}

    # --- Write updated content to file ---
    try:
        is_mini = tracker_type == "mini"; mini_tracker_start_index = -1; mini_tracker_end_index = -1; marker_start, marker_end = "", ""
        if is_mini and lines:
            mini_tracker_info = get_mini_tracker_data(); marker_start, marker_end = mini_tracker_info["markers"]
            try:
                mini_tracker_start_index = next(i for i, l in enumerate(lines) if l.strip() == marker_start)
                mini_tracker_end_index = next(i for i, l in enumerate(lines) if l.strip() == marker_end)
                if mini_tracker_start_index >= mini_tracker_end_index: raise ValueError("Start marker after end marker.")
            except (StopIteration, ValueError) as e: logger.warning(f"Mini markers invalid in {output_file}: {e}. Overwriting."); mini_tracker_start_index = -1
        with open(output_file, "w", encoding="utf-8", newline='\n') as f:
            # Preserve content before start marker
            if is_mini and mini_tracker_start_index != -1:
                for i in range(mini_tracker_start_index + 1): f.write(lines[i])
                if not lines[mini_tracker_start_index].endswith('\n'): f.write('\n')
            if is_mini and mini_tracker_start_index != -1: f.write("\n")
            # Write the updated tracker data section using hierarchical sort
            # Pass final_key_defs (map) and the final_sorted_keys_list
            _write_key_definitions(f, final_key_defs, final_sorted_keys_list)
            f.write("\n"); f.write(f"last_KEY_edit: {final_last_key_edit}\n"); f.write(f"last_GRID_edit: {final_last_grid_edit}\n\n")
            # Pass the final sorted list and final grid data
            _write_grid(f, final_sorted_keys_list, final_grid)
            # Preserve content after end marker
            if is_mini and mini_tracker_end_index != -1 and mini_tracker_start_index != -1:
                 f.write("\n");
                 for i in range(mini_tracker_end_index, len(lines)): f.write(lines[i])
            elif is_mini and mini_tracker_start_index == -1: f.write("\n" + marker_end + "\n")
        logger.info(f"Successfully updated tracker: {output_file}")
        # Invalidate caches
        invalidate_dependent_entries('tracker_data', f"tracker_data:{output_file}:.*")
        invalidate_dependent_entries('grid_decompress', '.*'); invalidate_dependent_entries('grid_validation', '.*'); invalidate_dependent_entries('grid_dependencies', '.*')
    except IOError as e: logger.error(f"I/O Error updating tracker file {output_file}: {e}", exc_info=True)
    except Exception as e: logger.exception(f"Unexpected error updating tracker file {output_file}: {e}")

# --- Export Tracker ---
def export_tracker(tracker_path: str, output_format: str = "json", output_path: Optional[str] = None) -> str:
    """
    Export a tracker file to various formats (json, csv, dot, md).

    Args:
        tracker_path: Path to the tracker file
        output_format: Format to export to ('md', 'json', 'csv', 'dot')
        output_path: Optional path to save the exported file
    Returns:
        Path to the exported file or error message string
    """
    tracker_path = normalize_path(tracker_path); check_file_modified(tracker_path) # Check cache validity
    logger.info(f"Attempting to export '{os.path.basename(tracker_path)}' to format '{output_format}'")
    tracker_data = read_tracker_file(tracker_path)
    if not tracker_data or not tracker_data.get("keys"): msg = f"Error: Cannot export empty/unreadable tracker: {tracker_path}"; logger.error(msg); return msg
    if output_path is None: base_name = os.path.splitext(tracker_path)[0]; output_path = normalize_path(f"{base_name}_export.{output_format}")
    else: output_path = normalize_path(output_path)
    try:
        dirname = os.path.dirname(output_path); os.makedirs(dirname, exist_ok=True)
        keys_map = tracker_data.get("keys", {}); grid = tracker_data.get("grid", {})
        # <<< *** MODIFIED SORTING *** >>>
        sorted_keys_list = sort_key_strings_hierarchically(list(keys_map.keys()))
        if output_format == "md": shutil.copy2(tracker_path, output_path)
        elif output_format == "json": # (JSON export unchanged)
            export_data = tracker_data.copy()
            with open(output_path, 'w', encoding='utf-8') as f: json.dump(export_data, f, indent=2, ensure_ascii=False)
        elif output_format == "csv": # (CSV export unchanged)
             with open(output_path, 'w', encoding='utf-8', newline='') as f:
                import csv; writer = csv.writer(f); writer.writerow(["Source Key", "Source Path", "Target Key", "Target Path", "Dependency Type"])
                key_to_idx = {k: i for i, k in enumerate(sorted_keys_list)}
                for source_key in sorted_keys_list:
                    compressed_row = grid.get(source_key)
                    if compressed_row:
                        try:
                             decompressed_row = decompress(compressed_row)
                             if len(decompressed_row) == len(sorted_keys_list):
                                 for j, dep_type in enumerate(decompressed_row):
                                     if dep_type not in (EMPTY_CHAR, DIAGONAL_CHAR, PLACEHOLDER_CHAR):
                                         target_key = sorted_keys_list[j]
                                         writer.writerow([source_key, keys_map.get(source_key, ""), target_key, keys_map.get(target_key, ""), dep_type])
                             else: logger.warning(f"CSV Export: Row length mismatch for key '{source_key}'")
                        except Exception as e: logger.warning(f"CSV Export: Error processing row for '{source_key}': {e}")
        elif output_format == "dot":
             with open(output_path, 'w', encoding='utf-8') as f:
                f.write("digraph Dependencies {\n  rankdir=LR;\n"); f.write('  node [shape=box, style="filled", fillcolor="#EFEFEF", fontname="Arial"];\n'); f.write('  edge [fontsize=10, fontname="Arial"];\n\n')
                for key in sorted_keys_list: label_path = os.path.basename(keys_map.get(key, '')).replace('\\', '/').replace('"', '\\"'); label = f"{key}\\n{label_path}"; f.write(f'  "{key}" [label="{label}"];\n')
                f.write("\n")
                key_to_idx = {k: i for i, k in enumerate(sorted_keys_list)}
                for source_key in sorted_keys_list:
                     compressed_row = grid.get(source_key)
                     if compressed_row:
                        try:
                             decompressed_row = decompress(compressed_row)
                             if len(decompressed_row) == len(sorted_keys_list):
                                 for j, dep_type in enumerate(decompressed_row):
                                     if dep_type not in (EMPTY_CHAR, DIAGONAL_CHAR, PLACEHOLDER_CHAR):
                                         target_key = sorted_keys_list[j]; color = "black"; style = "solid"; arrowhead="normal"
                                         if dep_type == '>': color = "blue"
                                         elif dep_type == '<': color = "green"; arrowhead="oinv"
                                         elif dep_type == 'x': color = "red"; style="dashed"; arrowhead="odot"
                                         elif dep_type == 'd': color = "orange"
                                         elif dep_type == 's': color = "grey"; style="dotted"
                                         elif dep_type == 'S': color = "dimgrey"; style="bold"
                                         f.write(f'  "{source_key}" -> "{target_key}" [label="{dep_type}", color="{color}", style="{style}", arrowhead="{arrowhead}"];\n')
                             else: logger.warning(f"DOT Export: Row length mismatch for key '{source_key}'")
                        except Exception as e: logger.warning(f"DOT Export: Error processing row for '{source_key}': {e}")
                f.write("}\n")
        else: msg = f"Error: Unsupported export format '{output_format}'"; logger.error(msg); return msg
        logger.info(f"Successfully exported tracker to: {output_path}")
        return output_path
    except IOError as e: msg = f"Error exporting tracker: I/O Error - {str(e)}"; logger.error(msg, exc_info=True); return msg
    except ImportError as e: msg = f"Error exporting tracker: Missing library for format '{output_format}' - {str(e)}"; logger.error(msg); return msg
    except Exception as e: msg = f"Error exporting tracker: Unexpected error - {str(e)}"; logger.exception(msg); return msg

# --- Remove File from Tracker ---
# <<< *** MODIFIED SIGNATURE AND LOGIC *** >>>
def remove_file_from_tracker(output_file: str, file_to_remove: str, path_to_key_info: Dict[str, KeyInfo]):
    """Removes a file's key and row/column from the tracker using path_to_key_info. Invalidates relevant caches."""
    output_file = normalize_path(output_file)
    file_to_remove_norm = normalize_path(file_to_remove)

    if not os.path.exists(output_file): logger.error(f"Tracker file '{output_file}' not found for removal."); raise FileNotFoundError(f"Tracker file '{output_file}' not found.")

    logger.info(f"Attempting to remove file '{file_to_remove_norm}' from tracker '{output_file}'")
    backup_tracker_file(output_file)

    lines = []
    try:
        with open(output_file, "r", encoding="utf-8") as f: lines = f.readlines()
    except Exception as e: logger.error(f"Failed to read tracker file {output_file} for removal: {e}", exc_info=True); raise IOError(f"Failed to read tracker file {output_file}: {e}") from e

    # Read existing data
    existing_key_defs = _read_existing_keys(lines)
    existing_grid = _read_existing_grid(lines)

    # Find the key to remove using the GLOBAL path_to_key_info map
    key_to_remove = get_key_string_from_path(file_to_remove_norm, path_to_key_info)

    # Also check if the key actually exists in *this* tracker's definitions
    if key_to_remove is None or key_to_remove not in existing_key_defs:
        logger.warning(f"File '{file_to_remove_norm}' (Key: {key_to_remove or 'Not Found Globally'}) not found in tracker definitions '{output_file}'. No changes made.")
        return

    logger.info(f"Found key '{key_to_remove}' for file '{file_to_remove_norm}'. Proceeding with removal from '{os.path.basename(output_file)}'.")

    # --- Prepare updated data ---
    final_key_defs = {k: v for k, v in existing_key_defs.items() if k != key_to_remove}
    # <<< *** MODIFIED SORTING *** >>>
    final_sorted_keys_list = sort_key_strings_hierarchically(list(final_key_defs.keys()))

    final_last_key_edit = f"Removed key: {key_to_remove} ({os.path.basename(file_to_remove_norm)})"
    final_last_grid_edit = f"Grid adjusted for removal of key: {key_to_remove}"

    # Rebuild grid without the removed key/row/column
    final_grid = {}
    # <<< *** MODIFIED SORTING *** >>>
    old_keys_list = sort_key_strings_hierarchically(list(existing_key_defs.keys()))
    try:
        idx_to_remove = old_keys_list.index(key_to_remove)
    except ValueError:
        logger.error(f"Key '{key_to_remove}' not found in old sorted list during removal grid update. Using filtered grid (might be incomplete).")
        final_grid = {k:v for k,v in existing_grid.items() if k != key_to_remove}
    else:
        for old_row_key, compressed_row in existing_grid.items():
             if old_row_key != key_to_remove: # Keep rows not being removed
                 try:
                     decomp_row = list(decompress(compressed_row))
                     if len(decomp_row) == len(old_keys_list):
                          # Remove the character at the removed key's index
                          new_decomp_row_list = decomp_row[:idx_to_remove] + decomp_row[idx_to_remove+1:]
                          final_grid[old_row_key] = compress("".join(new_decomp_row_list))
                     else: # Re-initialize row if length mismatch
                          logger.warning(f"Removal: Row length mismatch for key '{old_row_key}'. Re-initializing.")
                          row_list = [PLACEHOLDER_CHAR] * len(final_sorted_keys_list)
                          if old_row_key in final_sorted_keys_list: row_list[final_sorted_keys_list.index(old_row_key)] = DIAGONAL_CHAR
                          final_grid[old_row_key] = compress("".join(row_list))
                 except Exception as e: # Re-initialize row if decompression error
                      logger.warning(f"Removal: Error decompressing row for key '{old_row_key}': {e}. Re-initializing.")
                      row_list = [PLACEHOLDER_CHAR] * len(final_sorted_keys_list)
                      if old_row_key in final_sorted_keys_list: row_list[final_sorted_keys_list.index(old_row_key)] = DIAGONAL_CHAR
                      final_grid[old_row_key] = compress("".join(row_list))

    # --- Write updated file ---
    if write_tracker_file(output_file, final_key_defs, final_grid, final_last_key_edit, final_last_grid_edit):
         logger.info(f"Successfully removed key '{key_to_remove}' and file '{file_to_remove_norm}' from tracker '{output_file}'")
    else:
         logger.error(f"Failed to write updated tracker file after removal: {output_file}")
         # Consider restoring backup? Or raise error? For now, just log the failure.
         raise IOError(f"Failed to write updated tracker file {output_file} after removal.")

def remove_key_from_tracker(output_file: str, key_to_remove: str):
    """
    Removes a key string and its corresponding row/column from a specific tracker file.
    Invalidates relevant caches. Operates locally on the provided file and key string.

    Args:
        output_file: Path to the tracker file.
        key_to_remove: The key string to remove from this tracker.
    """
    output_file = normalize_path(output_file)

    if not os.path.exists(output_file): logger.error(f"Tracker file '{output_file}' not found for removal."); raise FileNotFoundError(f"Tracker file '{output_file}' not found.")

    logger.info(f"Attempting to remove key '{key_to_remove}' from tracker '{output_file}'")
    backup_tracker_file(output_file) # Ensure backup function call is present

    lines = []
    try:
        with open(output_file, "r", encoding="utf-8") as f: lines = f.readlines()
    except Exception as e: logger.error(f"Failed to read tracker file {output_file} for removal: {e}", exc_info=True); raise IOError(f"Failed to read tracker file {output_file}: {e}") from e

    existing_key_defs = _read_existing_keys(lines)
    existing_grid = _read_existing_grid(lines)

    if key_to_remove not in existing_key_defs:
        logger.warning(f"Key '{key_to_remove}' not found in tracker definitions '{output_file}'. No changes made.")
        return # Exit if key not found locally

    path_removed = existing_key_defs.get(key_to_remove, "Unknown Path")
    logger.info(f"Found key '{key_to_remove}' (Path: {path_removed}). Proceeding with removal from '{os.path.basename(output_file)}'.")

    # --- Prepare updated data ---
    final_key_defs = {k: v for k, v in existing_key_defs.items() if k != key_to_remove}
    final_sorted_keys_list = sort_key_strings_hierarchically(list(final_key_defs.keys())) # Use standard sort

    final_last_key_edit = f"Removed key: {key_to_remove} (Path: {path_removed})"
    final_last_grid_edit = f"Grid adjusted for removal of key: {key_to_remove}"

    # Rebuild grid without the removed key/row/column
    final_grid = {}
    old_keys_list = sort_key_strings_hierarchically(list(existing_key_defs.keys())) # Use standard sort
    try:
        idx_to_remove = old_keys_list.index(key_to_remove)
    except ValueError:
        logger.error(f"Key '{key_to_remove}' not found in old sorted list during grid update. Using filtered grid (might be incomplete).")
        final_grid = {k:v for k,v in existing_grid.items() if k != key_to_remove}
    else:
        for old_row_key, compressed_row in existing_grid.items():
             if old_row_key != key_to_remove:
                 try:
                     decomp_row = list(decompress(compressed_row))
                     if len(decomp_row) == len(old_keys_list):
                          new_decomp_row_list = decomp_row[:idx_to_remove] + decomp_row[idx_to_remove+1:]
                          final_grid[old_row_key] = compress("".join(new_decomp_row_list))
                     else: # Re-initialize row if length mismatch
                          logger.warning(f"Removal: Row length mismatch for key '{old_row_key}'. Re-initializing.")
                          row_list = [PLACEHOLDER_CHAR] * len(final_sorted_keys_list)
                          if old_row_key in final_sorted_keys_list: row_list[final_sorted_keys_list.index(old_row_key)] = DIAGONAL_CHAR
                          final_grid[old_row_key] = compress("".join(row_list))
                 except Exception as e: # Re-initialize row if decompression error
                      logger.warning(f"Removal: Error decompressing row for key '{old_row_key}': {e}. Re-initializing.")
                      row_list = [PLACEHOLDER_CHAR] * len(final_sorted_keys_list)
                      if old_row_key in final_sorted_keys_list: row_list[final_sorted_keys_list.index(old_row_key)] = DIAGONAL_CHAR
                      final_grid[old_row_key] = compress("".join(row_list))

    # --- Write updated file ---
    if write_tracker_file(output_file, final_key_defs, final_grid, final_last_key_edit, final_last_grid_edit):
         logger.info(f"Successfully removed key '{key_to_remove}' from tracker '{output_file}'")
         # Invalidate cache for this tracker
         invalidate_dependent_entries('tracker_data', f"tracker_data:{output_file}:.*")
         invalidate_dependent_entries('grid_decompress', '.*'); invalidate_dependent_entries('grid_validation', '.*'); invalidate_dependent_entries('grid_dependencies', '.*')

    else:
         logger.error(f"Failed to write updated tracker file after removal: {output_file}")
         raise IOError(f"Failed to write updated tracker file {output_file} after removal.")
    
# --- End of tracker_io.py ---