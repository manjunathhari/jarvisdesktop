//! Actions the brain may ask for, and the registry that guards them (ADR-002/003).

use crate::screen::{PanelUpdate, ScreenState};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::Value;

/// An action a module offers. Becomes a tool the brain can call.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ActionSpec {
    pub name: String,
    pub description: String,
    /// JSON Schema (object) for the arguments.
    pub input_schema: Value,
}

/// A concrete request from the brain.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Action {
    pub name: String,
    #[serde(default)]
    pub args: Value,
}

impl Action {
    pub fn new(name: &str, args: Value) -> Self {
        Self {
            name: name.into(),
            args,
        }
    }
}

/// What a module hands back after handling an action.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ModuleOutput {
    pub updates: Vec<PanelUpdate>,
    /// Something JARVIS should say.
    pub speech: Option<String>,
    /// Further actions to run (e.g. showing news also starts live TV).
    pub follow_up: Vec<Action>,
}

#[derive(Debug, thiserror::Error, PartialEq)]
pub enum ActionError {
    #[error("unknown action `{0}`")]
    Unknown(String),
    #[error("invalid arguments for `{action}`: {reason}")]
    InvalidArgs { action: String, reason: String },
    /// A failure the user should hear about, in plain words.
    #[error("{0}")]
    Failed(String),
}

#[async_trait]
pub trait Module: Send + Sync {
    fn actions(&self) -> Vec<ActionSpec>;
    async fn handle(
        &self,
        action: &Action,
        screen: &ScreenState,
    ) -> Result<ModuleOutput, ActionError>;
}

/// Lets one module be shared, e.g. registered *and* refreshed in the background.
#[async_trait]
impl<M: Module + ?Sized> Module for std::sync::Arc<M> {
    fn actions(&self) -> Vec<ActionSpec> {
        (**self).actions()
    }
    async fn handle(
        &self,
        action: &Action,
        screen: &ScreenState,
    ) -> Result<ModuleOutput, ActionError> {
        (**self).handle(action, screen).await
    }
}

/// Holds every module and refuses anything not registered.
#[derive(Default)]
pub struct Registry {
    modules: Vec<Box<dyn Module>>,
}

impl Registry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register(&mut self, module: Box<dyn Module>) {
        self.modules.push(module);
    }

    pub fn specs(&self) -> Vec<ActionSpec> {
        self.modules.iter().flat_map(|m| m.actions()).collect()
    }

    pub async fn dispatch(
        &self,
        action: &Action,
        screen: &ScreenState,
    ) -> Result<ModuleOutput, ActionError> {
        for m in &self.modules {
            if let Some(spec) = m.actions().into_iter().find(|s| s.name == action.name) {
                validate_args(&spec.input_schema, &action.args).map_err(|reason| {
                    ActionError::InvalidArgs {
                        action: action.name.clone(),
                        reason,
                    }
                })?;
                return m.handle(action, screen).await;
            }
        }
        Err(ActionError::Unknown(action.name.clone()))
    }
}

/// Minimal JSON-schema check: object args, required keys present,
/// declared property types (string / integer / number / boolean) respected,
/// `enum` values respected, and no undeclared keys.
pub fn validate_args(schema: &Value, args: &Value) -> Result<(), String> {
    let empty = serde_json::Map::new();
    let obj = match args {
        Value::Null => &empty,
        Value::Object(o) => o,
        _ => return Err("arguments must be an object".into()),
    };
    let props = schema
        .get("properties")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    if let Some(req) = schema.get("required").and_then(Value::as_array) {
        for key in req.iter().filter_map(Value::as_str) {
            if !obj.contains_key(key) {
                return Err(format!("missing `{key}`"));
            }
        }
    }
    for (key, val) in obj {
        let Some(prop) = props.get(key) else {
            return Err(format!("unexpected `{key}`"));
        };
        let ok = match prop.get("type").and_then(Value::as_str) {
            Some("string") => val.is_string(),
            Some("integer") => val.is_i64() || val.is_u64(),
            Some("number") => val.is_number(),
            Some("boolean") => val.is_boolean(),
            _ => true,
        };
        if !ok {
            return Err(format!("`{key}` has the wrong type"));
        }
        if let Some(allowed) = prop.get("enum").and_then(Value::as_array) {
            if !allowed.contains(val) {
                return Err(format!("`{key}` must be one of {allowed:?}"));
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::{Arc, Mutex};

    struct Echo {
        seen: Arc<Mutex<Vec<Action>>>,
    }

    #[async_trait]
    impl Module for Echo {
        fn actions(&self) -> Vec<ActionSpec> {
            vec![ActionSpec {
                name: "open_story".into(),
                description: "Open a story by number".into(),
                input_schema: json!({
                    "type": "object",
                    "properties": { "number": { "type": "integer" } },
                    "required": ["number"]
                }),
            }]
        }
        async fn handle(
            &self,
            action: &Action,
            _: &ScreenState,
        ) -> Result<ModuleOutput, ActionError> {
            self.seen.lock().unwrap().push(action.clone());
            Ok(ModuleOutput {
                speech: Some("ok".into()),
                ..Default::default()
            })
        }
    }

    fn registry() -> (Registry, Arc<Mutex<Vec<Action>>>) {
        let seen = Arc::new(Mutex::new(vec![]));
        let mut r = Registry::new();
        r.register(Box::new(Echo { seen: seen.clone() }));
        (r, seen)
    }

    #[tokio::test]
    async fn dispatches_a_valid_action_to_its_module() {
        let (r, seen) = registry();
        let out = r
            .dispatch(
                &Action::new("open_story", json!({"number": 4})),
                &ScreenState::new(),
            )
            .await;
        assert_eq!(out.unwrap().speech.as_deref(), Some("ok"));
        assert_eq!(seen.lock().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn refuses_actions_no_module_registered() {
        let (r, seen) = registry();
        let out = r
            .dispatch(&Action::new("delete_files", json!({})), &ScreenState::new())
            .await;
        assert_eq!(out, Err(ActionError::Unknown("delete_files".into())));
        assert!(seen.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn refuses_bad_arguments_before_the_module_sees_them() {
        let (r, seen) = registry();
        let out = r
            .dispatch(
                &Action::new("open_story", json!({"number": "four"})),
                &ScreenState::new(),
            )
            .await;
        assert!(
            matches!(out, Err(ActionError::InvalidArgs { .. })),
            "{out:?}"
        );
        assert!(seen.lock().unwrap().is_empty());
    }

    #[test]
    fn validate_requires_required_keys() {
        let s = json!({"type":"object","properties":{"n":{"type":"integer"}},"required":["n"]});
        assert!(validate_args(&s, &json!({})).is_err());
        assert!(validate_args(&s, &json!({"n": 2})).is_ok());
    }

    #[test]
    fn validate_checks_types_enums_and_unknown_keys() {
        let s = json!({"type":"object","properties":{
            "region":{"type":"string","enum":["india","world","all"]},
            "topic":{"type":"string"}
        }});
        assert!(validate_args(&s, &json!({})).is_ok(), "all optional");
        assert!(validate_args(&s, &json!({"region":"india","topic":"cricket"})).is_ok());
        assert!(
            validate_args(&s, &json!({"region":"mars"})).is_err(),
            "enum"
        );
        assert!(validate_args(&s, &json!({"topic": 5})).is_err(), "type");
        assert!(
            validate_args(&s, &json!({"rm":"-rf"})).is_err(),
            "undeclared key"
        );
        assert!(
            validate_args(&s, &json!("india")).is_err(),
            "args must be an object"
        );
    }

    #[test]
    fn null_args_count_as_empty_object() {
        let s = json!({"type":"object","properties":{}});
        assert!(validate_args(&s, &Value::Null).is_ok());
    }
}
