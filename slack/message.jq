# The Slack message of a release: a headline, the facts one per line, the next step and link buttons.
# Arguments: kind (released, stopped, failed), headline, version, check (the check's line file), translations (a line
# on missing ones, or empty), details, next, run and updates (links, updates may be empty)
def escape: gsub("&"; "&amp;") | gsub("<"; "&lt;") | gsub(">"; "&gt;");
def section($text): {type: "section", text: {type: "mrkdwn", text: ($text | escape | .[0:2900])}};
def button($label; $url): {type: "button", text: {type: "plain_text", text: $label}, url: $url};

($check | split("\n") | map(select(length > 0))) as $lines
| ({released: "✅", stopped: "⛔", failed: "❌"}[$kind] + " " + $headline) as $title
| ([
    (select($version != "") | "Version: `\($version)`"),
    # With the failed modules listed below, the first line only counts them
    (select(($lines | length) > 0) | "Install check: " + ($lines[0] | if ($lines | length) > 1
      then sub(", (?<count>[0-9]+) fail: .*?(?<rest>, [0-9]+ not checked)?\\.$"; ", \(.count) failed\(.rest // "")")
      else rtrimstr(".") end)),
    ($lines[1:][] | if startswith("`") then "• " + . else . end),
    (select($translations != "") | "⚠️ " + $translations),
    (select($details != "") | $details)
  ] | join("\n")) as $facts
| {
    # What the notification shows
    text: $title,
    blocks: [
      {type: "header", text: {type: "plain_text", text: $title[0:150], emoji: true}},
      (select($facts != "") | section($facts)),
      section($next),
      {type: "actions", elements: [
        (select($updates != "") | button("Updates page"; $updates) + {style: "primary"}),
        button("Open run"; $run)
      ]}
    ]
  }
