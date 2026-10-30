use std::cmp::Ordering;

fn effective(score: f32, boost: f32) -> (f64, f64) {
    debug_assert!(score.is_finite() && boost.is_finite());
    let a = f64::from(score);
    let b = f64::from(boost);
    let hi = a + b;
    let b_virtual = hi - a;
    let lo = (a - (hi - b_virtual)) + (b - b_virtual);
    (
        if hi == 0.0 { 0.0 } else { hi },
        if lo == 0.0 { 0.0 } else { lo },
    )
}

pub fn cmp_effective(a_score: f32, a_boost: f32, b_score: f32, b_boost: f32) -> Ordering {
    let a = effective(a_score, a_boost);
    let b = effective(b_score, b_boost);
    a.0.total_cmp(&b.0).then_with(|| a.1.total_cmp(&b.1))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn huge_boost_does_not_collapse_distinct_base_scores() {
        assert_eq!(f64::from(1e30_f32) + 1.0, f64::from(1e30_f32) + 2.0);
        assert_eq!(cmp_effective(1.0, 1e30, 2.0, 1e30), Ordering::Less);
        assert_eq!(
            cmp_effective(-2.0, f32::MAX, -1.0, f32::MAX),
            Ordering::Less
        );
        assert_eq!(
            cmp_effective(f32::from_bits(1), f32::MAX, 0.0, f32::MAX),
            Ordering::Greater
        );
    }

    #[test]
    fn mathematical_cross_bucket_ties_and_signed_zero_are_equal() {
        assert_eq!(cmp_effective(1.0, 2.0, 3.0, 0.0), Ordering::Equal);
        assert_eq!(
            cmp_effective(-f32::MAX, f32::MAX, -0.0, 0.0),
            Ordering::Equal
        );
        assert_eq!(cmp_effective(-0.0, -0.0, 0.0, 0.0), Ordering::Equal);
        assert_eq!(
            cmp_effective(-f32::from_bits(1), f32::from_bits(1), 0.0, 0.0),
            Ordering::Equal
        );
    }

    #[test]
    fn subnormals_and_cancellation_keep_the_exact_residual() {
        let tiny = f32::from_bits(1);
        assert_eq!(cmp_effective(tiny, tiny, tiny, 0.0), Ordering::Greater);
        assert_eq!(cmp_effective(-tiny, tiny, tiny, 0.0), Ordering::Less);
        assert_eq!(
            cmp_effective(-f32::MAX, f32::MAX, tiny, 0.0),
            Ordering::Less
        );
        assert_eq!(
            cmp_effective(f32::MAX, f32::MAX, f32::MAX, 0.0),
            Ordering::Greater
        );
    }

    #[test]
    fn constant_bucket_addition_preserves_strict_base_order() {
        let values = [
            -f32::MAX,
            -1e30,
            -2.0,
            -1.0,
            -f32::MIN_POSITIVE,
            -f32::from_bits(1),
            0.0,
            f32::from_bits(1),
            f32::MIN_POSITIVE,
            1.0,
            2.0,
            1e30,
            f32::MAX,
        ];
        for boost in [0.0, f32::from_bits(1), 1.0, 1e30, f32::MAX] {
            for pair in values.windows(2) {
                assert_eq!(
                    cmp_effective(pair[0], boost, pair[1], boost),
                    Ordering::Less,
                    "{:?}+{:?}",
                    pair,
                    boost
                );
            }
        }
    }
}
