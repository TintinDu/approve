from approve import parse_search
from conftest import URL_RE, mr_url

HEADER = "# Search Results for: \n\n## Messages ({n} results)\n"


def block(n, total, channel, channel_id, sender, ts, text):
    return (
        f"### Result {n} of {total}\nChannel: #{channel} (ID: {channel_id})\n"
        f"From: {sender}\nTime: 2026-11-04 15:17:21 CET\nMessage_ts: {ts}\n"
        f"Permalink: [link](https://x.slack.com/archives/{channel_id}/p1)\nText: \n{text}\n\n---\n\n"
    )


def search(*blocks):
    return HEADER.format(n=len(blocks)) + "".join(blocks)


HUMAN = "Alice EXAMPLE <alice@example.com> (ID: U0ALICE001) "


def test_extracts_channel_ts_and_exact_url():
    text = search(block(1, 1, "front", "C1", HUMAN, "1.5", f"review <{mr_url(7)}|MR !7>"))
    assert parse_search(text, URL_RE) == [
        {"channel": "front", "channel_id": "C1", "ts": "1.5", "urls": [mr_url(7)]}
    ]


def test_bundled_message_lists_every_url_once():
    body = "\n".join(f"<{mr_url(i)}|!{i}>" for i in (3, 1, 2)) + f"\n<{mr_url(1)}|again>"
    [msg] = parse_search(search(block(1, 1, "front", "C1", HUMAN, "1.5", body)), URL_RE)
    assert msg["urls"] == [mr_url(1), mr_url(2), mr_url(3)]


def test_bot_messages_are_ignored():
    bot = "  (ID: U00)  [BOT]"
    text = search(
        block(1, 2, "gitlab-activity", "C2", bot, "1.1", f"approval <{mr_url(7)}|!7>"),
        block(2, 2, "front", "C1", HUMAN, "1.2", f"<{mr_url(7)}|!7>"),
    )
    assert [m["channel"] for m in parse_search(text, URL_RE)] == ["front"]


def test_label_without_scheme_is_not_a_url():
    text = search(block(1, 1, "front", "C1", HUMAN, "1.5", "fix gitlab.test/grp/…/12345"))
    assert parse_search(text, URL_RE)[0]["urls"] == []


def test_url_of_another_host_is_not_matched():
    text = search(block(1, 1, "front", "C1", HUMAN, "1.5", "https://evil.test/grp/proj/-/merge_requests/9"))
    assert parse_search(text, URL_RE)[0]["urls"] == []


def test_no_results():
    assert parse_search("# Search Results for: \n\nNo results found.\n", URL_RE) == []


def test_block_without_timestamp_is_skipped():
    text = "### Result 1 of 1\nChannel: #front (ID: C1)\nFrom: x (ID: U1)\nText: \nhi\n"
    assert parse_search(text, URL_RE) == []
