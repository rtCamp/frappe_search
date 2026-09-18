import frappe
from frappe.utils import cint, escape_html
from frappe.utils.caching import redis_cache

# Hard ceiling on the row count a caller may ask for. Every returned row costs a
# document load in the per-row permission check, so the caller must not choose it.
MAX_SEARCH_LIMIT = 100

# Scoring constants
SEQUENTIAL_BONUS = 40
SEPARATOR_BONUS = 50
CAMEL_BONUS = 40
FIRST_LETTER_BONUS = 30
EXACT_MATCH_BONUS = 100


def strict_fuzzyx_match(pattern, text):
    """
    Strict fuzzy matching with enhanced scoring
    Returns (matched: bool, score: int, matches: list of indices)
    """
    pattern = pattern.lower() if pattern else ""
    text_lower = text.lower() if text else ""

    if not pattern or not text:
        return False, 0, []

    # First, try exact substring match (highest priority)
    if pattern in text_lower:
        start_idx = text_lower.find(pattern)
        matches = list(range(start_idx, start_idx + len(pattern)))
        # Score based on position and context
        position_score = max(300 - start_idx * 2, 200)
        if start_idx == 0 or text[start_idx - 1] in (
            " ",
            "_",
            "-",
            ".",
            "|",
            ",",
            ";",
            ":",
        ):
            position_score += EXACT_MATCH_BONUS
        return True, position_score, matches

    # Then try fuzzy matching
    best_matches = []
    best_score = 0

    for start_pos in range(len(text)):
        if text_lower[start_pos] != pattern[0]:
            continue

        matches = []
        pattern_idx = 0
        text_idx = start_pos
        gaps = 0

        while pattern_idx < len(pattern) and text_idx < len(text):
            if pattern[pattern_idx] == text_lower[text_idx]:
                matches.append(text_idx)
                pattern_idx += 1
                gaps = 0
            else:
                gaps += 1
                if gaps > 2:
                    break
            text_idx += 1

        if pattern_idx == len(pattern):
            score = calculate_enhanced_score(pattern, text, matches, start_pos)
            if score > 30 and (not best_matches or score > best_score):
                best_matches = matches
                best_score = score

    if best_matches:
        return True, best_score, best_matches
    return False, 0, []


def calculate_enhanced_score(pattern, text, matches, start_pos):
    """Enhanced scoring system"""
    if not matches or len(matches) != len(pattern):
        return 0

    score = 50 + len(pattern) * 10

    # Position bonuses
    if start_pos == 0:
        score += 60
    elif start_pos <= 5:
        score += 40
    elif start_pos <= 20:
        score += 20
    else:
        score += min(start_pos * -1.5, -40)

    # Word boundary bonus
    if start_pos == 0 or text[start_pos - 1] in (
        " ",
        "_",
        "-",
        ".",
        "|",
        ",",
        ";",
        ":",
        "\n",
        "\t",
    ):
        score += SEPARATOR_BONUS

    end_pos = matches[-1]
    if end_pos == len(text) - 1 or text[end_pos + 1] in (
        " ",
        "_",
        "-",
        ".",
        "|",
        ",",
        ";",
        ":",
        "\n",
        "\t",
    ):
        score += 20

    # Consecutive character bonuses and gap penalties
    consecutive_chars = 0
    for i in range(len(matches)):
        if i > 0:
            gap = matches[i] - matches[i - 1] - 1
            if gap == 0:
                consecutive_chars += 1
                score += SEQUENTIAL_BONUS
            else:
                score += gap * gap * -2

    # Density bonus/penalty
    total_span = matches[-1] - matches[0] + 1
    density = len(pattern) / total_span
    if density < 0.5:
        score -= 40
    elif density > 0.8:
        score += 30

    # Context bonuses
    for match_pos in matches:
        if match_pos > 0:
            prev_char = text[match_pos - 1]
            curr_char = text[match_pos]
            if prev_char.islower() and curr_char.isupper():
                score += CAMEL_BONUS

        if match_pos == 0 or text[match_pos - 1] in (" ", "_", "-", ".", "|"):
            score += FIRST_LETTER_BONUS

    # Length ratio bonus
    length_ratio = len(pattern) / len(text)
    if length_ratio > 0.3:
        score += int(length_ratio * 100)

    return max(score, 0)


def find_all_strict_fuzzy_matches(pattern, text):
    """Find all strict fuzzy matches"""
    if not pattern or not text:
        return []

    all_matches = []
    pattern_lower = pattern.lower()
    text_lower = text.lower()

    # Find all exact matches first
    start = 0
    while True:
        pos = text_lower.find(pattern_lower, start)
        if pos == -1:
            break
        matches = list(range(pos, pos + len(pattern)))
        position_score = max(350 - pos * 2, 250)
        if pos == 0 or text[pos - 1] in (" ", "_", "-", ".", "|", ",", ";", ":"):
            position_score += EXACT_MATCH_BONUS
        all_matches.append((matches, position_score))
        start = pos + 1

    # If no exact matches, try fuzzy matching
    if not all_matches:
        matched, score, matches = strict_fuzzyx_match(pattern, text)
        if matched:
            all_matches.append((matches, score))

    return all_matches


def highlight_all_occurrences(text, search_terms):
    """Highlight high-quality matches.

    Returns an HTML-safe string: the document text is escaped and only the
    <mark> tags added here are live markup.
    """
    if not search_terms or not text:
        return escape_html(text or "")

    terms = [term.strip() for term in search_terms.split() if term.strip()]
    if not terms:
        return escape_html(text)

    highlighted_positions = set()

    for term in terms:
        matches_list = find_all_strict_fuzzy_matches(term, text)
        for matches, score in matches_list:
            if score > 40:
                highlighted_positions.update(matches)

    # Build highlighted string
    result = ""
    in_mark = False

    for i, char in enumerate(text):
        should_highlight = i in highlighted_positions

        if should_highlight and not in_mark:
            result += "<mark>"
            in_mark = True
        elif not should_highlight and in_mark:
            result += "</mark>"
            in_mark = False

        # escape per character: the match offsets index the *unescaped* string,
        # so escaping the whole text up front would shift every index
        result += escape_html(char)

    if in_mark:
        result += "</mark>"

    return result


def extract_context_around_matches(text, matches, context_chars=35):
    """Extract smart context around matches"""
    if not matches or not text:
        return text[:80] + ("..." if len(text) > 80 else "")

    min_match = min(matches)
    max_match = max(matches)

    effective_context = min(context_chars, len(text) // 4)
    start = max(0, min_match - effective_context)
    end = min(len(text), max_match + effective_context)

    # Smart boundary detection
    if start > 0:
        for i in range(start, min(start + 25, min_match)):
            if text[i] in (" ", "\n", ".", "!", "?", "|", ",", ";", ":", "-", "(", ")"):
                start = i + 1
                break

    if end < len(text):
        for i in range(end, min(end + 25, len(text))):
            if text[i] in (" ", "\n", ".", "!", "?", "|", ",", ";", ":", "-", "(", ")"):
                end = i
                break

    context = text[start:end].strip()

    if start > 0:
        context = "..." + context
    if end < len(text):
        context = context + "..."

    if len(context) > 150:
        context = context[:150] + "..."

    return context


def fuzzy_search(keywords="", item="", return_marked_string=False):
    """Enhanced fuzzy search with scoring"""
    if not keywords or not item:
        if return_marked_string:
            item = item or ""
            return {
                "score": 0,
                "marked_string": escape_html(item),
                "context": escape_html(item[:80] + ("..." if len(item) > 80 else "")),
            }
        return 0

    terms = [term.strip() for term in keywords.split() if term.strip()]
    total_score = 0
    all_matches = []

    for term in terms:
        matches_list = find_all_strict_fuzzy_matches(term, item)
        if matches_list:
            best_score = max(score for matches, score in matches_list)
            total_score += best_score

            for matches, score in matches_list:
                if score > 40:
                    all_matches.extend(matches)

    # Multi-term bonus
    if len(terms) > 1 and total_score > 0:
        total_score += len(terms) * 20

    if not return_marked_string:
        return total_score

    if total_score <= 40:
        truncated_item = item[:80] + ("..." if len(item) > 80 else "")
        return {
            "score": 0,
            "marked_string": escape_html(item),
            "context": escape_html(truncated_item),
        }

    all_matches = sorted(list(set(all_matches)))
    context = extract_context_around_matches(item, all_matches, context_chars=25)
    marked_string = highlight_all_occurrences(item, keywords)
    highlighted_context = highlight_all_occurrences(context, keywords)

    return {
        "score": total_score,
        "marked_string": marked_string,
        "context": highlighted_context,
    }


def normalize_doctypes(value):
    """Normalise a caller-supplied doctype list into a hashable tuple.

    Accepts a real sequence, a JSON array, or a comma-separated string, because
    `frappe.form_dict` delivers a different type per transport. The result is a
    tuple so it stays hashable for `@redis_cache`, which keys on
    `frozenset(kwargs.items())`.
    """
    if not value:
        return ()

    if isinstance(value, str):
        try:
            value = frappe.parse_json(value)
        except Exception:
            value = value.split(",")
        if isinstance(value, str):
            value = [value]

    if not isinstance(value, list | tuple | set):
        return ()

    return tuple(str(d).strip() for d in value if str(d).strip())


def get_permitted_doctypes(requested=()):
    """The doctypes this session may search.

    Authorization is the server's decision, never the caller's: the set is the
    globally-indexed doctypes intersected with the user's own read rights.
    `requested` may *narrow* that set, it can never widen it.
    """
    from frappe.desk.doctype.global_search_settings.global_search_settings import (
        get_doctypes_for_global_search,
    )

    permitted = set(get_doctypes_for_global_search()) & set(
        frappe.get_user().get_can_read()
    )

    if requested:
        permitted &= set(requested)

    return tuple(sorted(permitted))


@frappe.whitelist()
def search(text: str, start: int = 0, limit: int = 20, doctype: str = ""):
    """Search for given text in __global_search"""
    return _search(
        text,
        start=max(cint(start), 0),
        limit=min(max(cint(limit), 1), MAX_SEARCH_LIMIT),
        doctype=doctype or "",
    )


@redis_cache(ttl=180, user=True)
def _search(text, start=0, limit=20, doctype="", allowed_doctypes=()):
    """Cached core of `search`.

    `allowed_doctypes` is an optional *narrowing* filter for in-process callers
    (see `get_permitted_doctypes`); it is not, and must never become, the source
    of authorization. Every argument is already normalised and clamped by the
    caller, so the cache key is always hashable.
    """
    from frappe.query_builder.functions import Match

    results = []
    sorted_results = []

    allowed_doctypes = get_permitted_doctypes(allowed_doctypes)

    if not allowed_doctypes or (doctype and doctype not in allowed_doctypes):
        return []

    for word in set(text.split("&")):
        word = word.strip()
        if not word:
            continue

        global_search = frappe.qb.Table("__global_search")
        rank = Match(global_search.content).Against(word)
        query = (
            frappe.qb.from_(global_search)
            .select(
                global_search.doctype,
                global_search.name,
                global_search.content,
                rank.as_("rank"),
            )
            .where(rank)
            .orderby("rank", order=frappe.qb.desc)
            .limit(limit)
        )

        if doctype:
            query = query.where(global_search.doctype == doctype)
        else:
            query = query.where(global_search.doctype.isin(allowed_doctypes))

        if cint(start) > 0:
            query = query.offset(start)

        result = query.run(as_dict=True)
        results.extend(result)

    # Sort results based on allowed_doctype's priority
    for doctype in allowed_doctypes:
        for r in results:
            if r.doctype == doctype and r.rank > 0.0:
                try:
                    # __global_search is read as a raw table, so no permission
                    # layer applies to the query -- re-check each row here
                    if not frappe.has_permission(r.doctype, "read", r.name):
                        continue

                    meta = frappe.get_meta(r.doctype)
                    if meta.title_field:
                        r.title = frappe.db.get_value(
                            r.doctype, r.name, meta.title_field
                        )
                except Exception:
                    frappe.clear_messages()
                    continue

                sorted_results.append(r)

    return sorted_results


@frappe.whitelist()
def get_global_search_results(
    text: str,
    start: int = 0,
    limit: int = 20,
    doctype: str = "",
    allowed_doctypes: list | None = None,
):
    start = max(cint(start), 0)
    limit = min(max(cint(limit), 1), MAX_SEARCH_LIMIT)
    doctype = doctype or ""
    # a narrowing filter only -- `_search` intersects it with the caller's rights
    allowed_doctypes_tuple = normalize_doctypes(allowed_doctypes)
    second_start = start + limit
    if len(text or "") < 3:
        return []

    search_results = process_results(
        start, limit, doctype, allowed_doctypes_tuple, text
    )
    # Pre-process next set of results to check if "load more" is needed.
    # Results are cached to prevent redundant searches.
    secondary_search_results = process_results(
        second_start, limit, doctype, allowed_doctypes_tuple, text
    )

    load_more = False
    if secondary_search_results:
        load_more = True

    return search_results, load_more


def process_results(start, limit, doctype, allowed_doctypes, text):
    results = _search(
        text,
        start=start,
        limit=limit,
        doctype=doctype,
        allowed_doctypes=allowed_doctypes,
    )

    if not results:
        return []

    # Group results by doctype for batch processing
    results_by_doctype = {}
    for result in results:
        if result.doctype not in results_by_doctype:
            results_by_doctype[result.doctype] = []
        results_by_doctype[result.doctype].append(result)

    # Batch check existence and permissions per doctype
    valid_docs = set()
    for dt, dt_results in results_by_doctype.items():
        names = [r.name for r in dt_results]
        try:
            # Batch fetch with permission check built-in (respects permission_query_conditions)
            existing = frappe.get_all(
                dt,
                filters={"name": ["in", names]},
                fields=["name"],
                limit_page_length=None  # Get all matching records
            )
            # Optimize f-string formatting in generator
            valid_docs.update(dt + ":" + d.name for d in existing)
        except frappe.PermissionError:
            # User has no access to this doctype at all
            continue
        except (frappe.DoesNotExistError, AttributeError):
            # Handle cases where doctype might not exist or has attribute issues
            frappe.clear_messages()
            continue
        except Exception as e:
            # Log unexpected exceptions for debugging
            frappe.log_error(
                f"Unexpected error in batch permission check for {dt}: {str(e)}",
                "Search Batch Permission Error"
            )
            frappe.clear_messages()
            continue

    processed_results = []

    for result in results:
        content = result.content
        if ("||| Name: " not in content) and not content.startswith("Name: "):
            content = f"Name: {result.name} ||| {content}"

        # `|||` is left in place until after escaping: it survives escape_html
        # untouched, so the <br> can be substituted into the safe output. Doing
        # it first would either escape the tag away or let the markup be matched
        # and split by a <mark>.
        fuzzy = fuzzy_search(text, content, return_marked_string=True)
        if fuzzy["score"] > 0:
            doc_key = f"{result.doctype}:{result.name}"
            if doc_key not in valid_docs:
                continue
            result.score = fuzzy["score"]
            result.content = escape_html(content).replace("|||", "<br>")
            result.marked_string = fuzzy["context"].replace("|||", "<br>")
            result.full_marked_string = fuzzy["marked_string"].replace("|||", "<br>")
            processed_results.append(result)

    return sorted(processed_results, key=lambda x: x.score, reverse=True)
