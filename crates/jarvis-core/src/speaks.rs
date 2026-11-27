/// Something that speaks aloud: the in-process `Voice`, or the voice helper behind a pipe.
pub trait Speaks: Send + Sync {
    /// Queue a sentence.
    fn speak(&self, text: &str);
    /// Stop what is playing and drop everything queued.
    fn stop(&self);
    /// Is speech playing right now?
    fn is_speaking(&self) -> bool;
}
