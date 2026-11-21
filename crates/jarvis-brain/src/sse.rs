//! Server-sent events: bytes in, `(event, data)` pairs out. Pure and incremental,
//! because network chunks can split an event anywhere.

#[derive(Debug, Default)]
pub struct SseParser {
    buf: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct SseEvent {
    pub event: String,
    pub data: String,
}

impl SseParser {
    pub fn new() -> Self {
        Self::default()
    }

    /// Feed a chunk; get back every event completed by it.
    pub fn push(&mut self, chunk: &str) -> Vec<SseEvent> {
        self.buf.push_str(chunk);
        if self.buf.contains('\r') {
            self.buf = self.buf.replace("\r\n", "\n");
        }
        let mut out = Vec::new();
        while let Some(pos) = self.buf.find("\n\n") {
            let block: String = self.buf.drain(..pos + 2).collect();
            let mut event = String::new();
            let mut data: Vec<&str> = Vec::new();
            for line in block.lines() {
                if let Some(v) = line.strip_prefix("event:") {
                    event = v.trim().to_string();
                } else if let Some(v) = line.strip_prefix("data:") {
                    data.push(v.strip_prefix(' ').unwrap_or(v));
                }
            }
            if !event.is_empty() || !data.is_empty() {
                out.push(SseEvent {
                    event,
                    data: data.join("\n"),
                });
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_complete_events() {
        let mut p = SseParser::new();
        let evs = p.push(
            "event: ping\ndata: {}\n\nevent: message_stop\ndata: {\"type\":\"message_stop\"}\n\n",
        );
        assert_eq!(
            evs,
            vec![
                SseEvent {
                    event: "ping".into(),
                    data: "{}".into()
                },
                SseEvent {
                    event: "message_stop".into(),
                    data: "{\"type\":\"message_stop\"}".into()
                },
            ]
        );
    }

    #[test]
    fn events_split_across_chunks_are_reassembled() {
        let mut p = SseParser::new();
        assert!(p.push("event: content_block_delta\nda").is_empty());
        assert!(p.push("ta: {\"a\":1}\n").is_empty());
        let evs = p.push("\n");
        assert_eq!(
            evs,
            vec![SseEvent {
                event: "content_block_delta".into(),
                data: "{\"a\":1}".into()
            }]
        );
    }

    #[test]
    fn handles_crlf_and_multiline_data() {
        let mut p = SseParser::new();
        let evs = p.push("event: x\r\ndata: line1\r\ndata: line2\r\n\r\n");
        assert_eq!(
            evs,
            vec![SseEvent {
                event: "x".into(),
                data: "line1\nline2".into()
            }]
        );
    }
}
