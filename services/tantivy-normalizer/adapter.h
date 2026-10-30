#ifndef TYPEAHEAD_NORMALIZER_H
#define TYPEAHEAD_NORMALIZER_H
#include <stddef.h>
#include <stdint.h>
typedef struct {
    const uint8_t *data;
    size_t len;
    void *owner;
} TaResult;
/* ABI return 0 means valid JSON reply; inspect reply.status for query errors.
   Nonzero means ABI failure and an empty result. No input pointers are retained.
   The caller must zero-initialize TaResult, call free exactly once after success,
   and must not reuse the same result concurrently. */
int32_t ta_plan(const uint8_t *input, size_t len, TaResult *out);
void ta_result_free(TaResult *result);
const char *ta_normalizer_version(void);
const char *ta_plan_version(void);
/* Float conversion uses canonical f32 rounding; nonfinite or overflowing inputs
   return nonzero rather than producing an unrankable score. */
int32_t ta_score_f32(double input, float *value, uint32_t *bits);
#endif
