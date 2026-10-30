use std::cmp::Ordering;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Exact {
    negative: bool,
    limbs: [u64; 5],
}
impl Exact {
    pub fn from_f32(value: f32) -> Self {
        assert!(value.is_finite());
        let bits = value.to_bits();
        let exponent = (bits >> 23) & 255;
        let significand = u64::from(bits & 0x7fffff) | if exponent == 0 { 0 } else { 1 << 23 };
        let shift = if exponent == 0 { 0 } else { exponent - 1 } as usize;
        let mut limbs = [0; 5];
        limbs[shift / 64] = significand << (shift % 64);
        if shift % 64 > 40 {
            limbs[shift / 64 + 1] = significand >> (64 - shift % 64);
        }
        Self {
            negative: bits >> 31 != 0 && significand != 0,
            limbs,
        }
    }
    fn magnitude(&self, other: &Self) -> Ordering {
        self.limbs.iter().rev().cmp(other.limbs.iter().rev())
    }
    pub fn add(self, other: Self) -> Self {
        if self.negative == other.negative {
            let mut out = self;
            let mut carry = false;
            for i in 0..5 {
                let (a, first) = self.limbs[i].overflowing_add(other.limbs[i]);
                let (b, second) = a.overflowing_add(u64::from(carry));
                out.limbs[i] = b;
                carry = first || second;
            }
            assert!(!carry);
            out
        } else {
            let (large, small) = if self.magnitude(&other).is_lt() {
                (other, self)
            } else {
                (self, other)
            };
            let mut out = large;
            let mut borrow = false;
            for i in 0..5 {
                let (a, first) = large.limbs[i].overflowing_sub(small.limbs[i]);
                let (b, second) = a.overflowing_sub(u64::from(borrow));
                out.limbs[i] = b;
                borrow = first || second;
            }
            assert!(!borrow);
            if out.limbs == [0; 5] {
                out.negative = false;
            }
            out
        }
    }
}
impl Ord for Exact {
    fn cmp(&self, other: &Self) -> Ordering {
        match (self.negative, other.negative) {
            (true, false) => Ordering::Less,
            (false, true) => Ordering::Greater,
            (true, true) => self.magnitude(other).reverse(),
            _ => self.magnitude(other),
        }
    }
}
impl PartialOrd for Exact {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exact_grid_and_cancellation() {
        let tiny = f32::from_bits(1);
        assert_eq!(Exact::from_f32(tiny).limbs, [1, 0, 0, 0, 0]);
        assert_eq!(
            Exact::from_f32(f32::MAX).add(Exact::from_f32(-f32::MAX)),
            Exact::from_f32(0.0)
        );
        assert!(
            Exact::from_f32(1e30).add(Exact::from_f32(2.0))
                > Exact::from_f32(1e30).add(Exact::from_f32(1.0))
        );
        assert!(Exact::from_f32(2.0).add(Exact::from_f32(tiny)) > Exact::from_f32(2.0));
        assert!(
            Exact::from_f32(f32::MAX).add(Exact::from_f32(f32::MAX)) > Exact::from_f32(f32::MAX)
        );
    }
}
