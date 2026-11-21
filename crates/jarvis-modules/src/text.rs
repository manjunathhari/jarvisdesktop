//! Turning feed HTML into short plain text. The UI only ever inserts text
//! (never HTML), so this is about readability, and a second safety layer.

/// Remove tags, decode common entities, collapse whitespace.
pub fn plain_text(html: &str) -> String {
    const BLOCK: &[&str] = &[
        "p", "br", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr",
    ];
    let mut out = String::with_capacity(html.len());
    let mut rest = html;
    while let Some(lt) = rest.find('<') {
        out.push_str(&rest[..lt]);
        let after = &rest[lt + 1..];
        let Some(gt) = after.find('>') else {
            // A lone '<' is text, not a tag.
            out.push('<');
            rest = after;
            continue;
        };
        let tag = &after[..gt];
        let name: String = tag
            .trim_start_matches('/')
            .chars()
            .take_while(|c| c.is_ascii_alphanumeric())
            .collect::<String>()
            .to_ascii_lowercase();
        rest = &after[gt + 1..];
        if !tag.starts_with('/') && (name == "script" || name == "style") {
            let close = format!("</{name}");
            match rest.to_ascii_lowercase().find(&close) {
                Some(i) => {
                    let tail = &rest[i..];
                    rest = tail.find('>').map(|j| &tail[j + 1..]).unwrap_or("");
                }
                None => rest = "",
            }
            out.push(' ');
        } else if BLOCK.contains(&name.as_str()) {
            out.push(' ');
        }
    }
    out.push_str(rest);
    decode_entities(&out)
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn decode_entities(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(amp) = rest.find('&') {
        out.push_str(&rest[..amp]);
        let after = &rest[amp + 1..];
        let decoded = after.find(';').filter(|&semi| semi <= 8).and_then(|semi| {
            let ent = &after[..semi];
            let ch = match ent {
                "amp" => Some('&'),
                "lt" => Some('<'),
                "gt" => Some('>'),
                "quot" => Some('"'),
                "apos" => Some('\''),
                "nbsp" => Some(' '),
                _ if ent.starts_with("#x") || ent.starts_with("#X") => {
                    u32::from_str_radix(&ent[2..], 16)
                        .ok()
                        .and_then(char::from_u32)
                }
                _ if ent.starts_with('#') => ent[1..].parse().ok().and_then(char::from_u32),
                _ => None,
            };
            ch.map(|c| (c, semi))
        });
        match decoded {
            Some((c, semi)) => {
                out.push(c);
                rest = &after[semi + 1..];
            }
            None => {
                out.push('&');
                rest = after;
            }
        }
    }
    out.push_str(rest);
    out
}

/// Cut to at most `max` characters, at a word boundary, adding "…".
pub fn truncate_words(text: &str, max: usize) -> String {
    if text.chars().count() <= max {
        return text.to_string();
    }
    let cut: String = text.chars().take(max).collect();
    let base = match cut.rfind(' ') {
        Some(i) if i > 0 => &cut[..i],
        _ => cut.as_str(),
    };
    format!("{}…", base.trim_end_matches([' ', ',', ';', ':']))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strips_tags_and_decodes_entities() {
        assert_eq!(
            plain_text("<p>The <b>monsoon</b> arrived &amp; rain fell.</p><img src='x'/>"),
            "The monsoon arrived & rain fell."
        );
        assert_eq!(
            plain_text("A&nbsp;&quot;quote&quot; &#39;x&#39; &lt;tag&gt;"),
            "A \"quote\" 'x' <tag>"
        );
    }

    #[test]
    fn drops_script_and_style_contents() {
        assert_eq!(
            plain_text("Hi<script>alert(1)</script> there<style>p{}</style>"),
            "Hi there"
        );
    }

    #[test]
    fn collapses_whitespace() {
        assert_eq!(plain_text("  a\n\n  b\tc  "), "a b c");
    }

    #[test]
    fn truncates_at_word_boundary() {
        assert_eq!(truncate_words("short", 50), "short");
        assert_eq!(
            truncate_words("the quick brown fox jumps", 12),
            "the quick…"
        );
    }

    #[test]
    fn truncate_is_safe_on_multibyte_text() {
        let s = "नमस्ते दुनिया नमस्ते दुनिया";
        let out = truncate_words(s, 8);
        assert!(out.ends_with('…'));
    }
}
