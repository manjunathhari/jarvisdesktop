//! Microphones deliver 44.1/48 kHz, often stereo; the models want 16 kHz mono.

/// Mix interleaved `channels` down to mono and resample `rate` → 16 kHz
/// (linear interpolation: plenty for speech recognition).
pub fn to_mono_16k(interleaved: &[f32], channels: u16, rate: u32) -> Vec<f32> {
    let ch = channels.max(1) as usize;
    let mono: Vec<f32> = if ch == 1 {
        interleaved.to_vec()
    } else {
        interleaved
            .chunks(ch)
            .map(|f| f.iter().sum::<f32>() / f.len() as f32)
            .collect()
    };
    if rate == 16_000 || mono.is_empty() {
        return mono;
    }
    let out_len = (mono.len() as u64 * 16_000 / rate as u64) as usize;
    let step = rate as f64 / 16_000.0;
    (0..out_len)
        .map(|i| {
            let pos = i as f64 * step;
            let j = pos.floor() as usize;
            let frac = (pos - j as f64) as f32;
            let a = mono[j.min(mono.len() - 1)];
            let b = mono[(j + 1).min(mono.len() - 1)];
            a + (b - a) * frac
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mono_16k_passes_through_unchanged() {
        let x = vec![0.1, -0.2, 0.3];
        assert_eq!(to_mono_16k(&x, 1, 16_000), x);
    }

    #[test]
    fn stereo_is_averaged_to_mono() {
        assert_eq!(
            to_mono_16k(&[0.2, 0.4, -1.0, 1.0], 2, 16_000),
            vec![0.3, 0.0]
        );
    }

    #[test]
    fn forty_eight_kilohertz_becomes_a_third_as_many_samples() {
        let x: Vec<f32> = (0..48_000).map(|i| i as f32).collect();
        let y = to_mono_16k(&x, 1, 48_000);
        assert_eq!(y.len(), 16_000);
        assert!((y[1] - 3.0).abs() < 1e-3, "every third sample: {}", y[1]);
    }

    #[test]
    fn forty_four_one_kilohertz_has_the_right_length() {
        let y = to_mono_16k(&vec![0.0; 44_100], 1, 44_100);
        assert_eq!(y.len(), 16_000);
    }

    #[test]
    fn empty_input_is_fine() {
        assert!(to_mono_16k(&[], 2, 48_000).is_empty());
    }
}
