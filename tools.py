import re
import typing as t


def split_query_from_http_target(target: str) -> t.Tuple[str, t.Dict]:
    """ Split the query part from the HTTP request target. Returns the target without a query
    and the query arguments as a separate dict.
    Information about http queries: RFC 3986, section 3.4.
    """
    lst = target.split("?")
    target = lst[0]
    query = dict()
    q_str = "".join(lst[1:])
    if q_str:
        for arg in q_str.split("&"):
            kv = arg.split("=")
            if len(kv) != 2:
                raise ValueError(f"wrong arg '{arg}' in target '{target}'")
            query[kv[0]] = kv[1]

    return target, query


def resolve_percents_in_http_target(target: str) -> str:
    """ Resolve percent-encoded strings (RFC 3986, section 2.1) """
    lst = target.split("%")
    if not len(lst):
        return target

    for i in range(len(lst)):
        m = re.match(r"([0-9A-Fa-f]{2})(.*)", lst[i])
        if m:
            lst[i] = chr(int(m[1], base=16)) + m[2]
    return "".join(lst)
