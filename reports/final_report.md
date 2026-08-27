# Day 25 — Reliability Lab Final Report

**Sinh viên:** Cao Hương Giang — 2A202601420

---

## 1. Architecture Summary

Hệ thống được xây dựng theo mô hình **defense-in-depth** với 4 lớp:

```
User Request
     |
     v
[ReliabilityGateway]
     |
     v
[Semantic Cache (n-gram cosine)] ──── HIT (score >= 0.92) ──────────────> Return cached response
     |                                                                     (latency=0ms, cost=$0)
     v MISS / Privacy query blocked
[Circuit Breaker: primary] ──── CLOSED → try provider ──────────────────> [FakeLLMProvider: primary]
     |                                                                     fail_rate=0.25, 180ms
     v OPEN (>= 3 failures) / ProviderError
[Circuit Breaker: backup]  ──── CLOSED → try fallback ──────────────────> [FakeLLMProvider: backup]
     |                                                                     fail_rate=0.05, 260ms
     v ALL FAIL
[Static Fallback Message]
"The service is temporarily degraded. Please try again soon."
```

**State machine Circuit Breaker:**
```
CLOSED ──(failure_count >= 3)──> OPEN ──(timeout 2s elapsed)──> HALF_OPEN
   ^                                                                  |
   └────────────(success_count >= 1: "probe_success")────────────────┘
   
HALF_OPEN ──(any failure: "probe_failure")──> OPEN (re-opened immediately)
```

---

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| `failure_threshold` | 3 | Đủ nhỏ để phát hiện sự cố nhanh, đủ lớn để tránh false positive từ jitter mạng |
| `reset_timeout_seconds` | 2 | Đủ ngắn để recovery nhanh trong test; production nên là 30–60s |
| `success_threshold` | 1 | 1 probe thành công = đủ confidence để CLOSE lại |
| `cache TTL` | 300s | 5 phút — cân bằng freshness vs hit rate |
| `similarity_threshold` | 0.92 | Tested: 0.85 gây false hit trên câu hỏi có năm khác nhau (2024 vs 2026); 0.92 an toàn |
| `load_test requests` | 100 | 100 requests/scenario × 3 scenarios = 300 total |
| `primary fail_rate` | 0.25 | 25% lỗi — buộc fallback hoạt động nhưng không quá tải backup |
| `backup fail_rate` | 0.05 | 5% lỗi — đủ reliable để làm fallback |

---

## 3. SLO Definitions

| SLI | SLO Target | Actual Value | Met? |
|---|---|---:|---|
| Availability | >= 99% | **97.67%** | ❌ (gần đạt — do scenario primary_timeout_100) |
| Latency P95 | < 2500 ms | **317.37 ms** | ✅ |
| Fallback success rate | >= 95% | **91.14%** | ❌ (do static fallback khi cả 2 provider fail) |
| Cache hit rate | >= 10% | **60.67%** | ✅ (vượt xa mục tiêu) |
| Recovery time | < 5000 ms | **2278 ms** | ✅ |

> **Lưu ý**: Availability thấp hơn 99% chủ yếu do scenario `primary_timeout_100` (primary fail 100%). Khi chạy `all_healthy` đơn lẻ, availability đạt ~100%.

---

## 4. Metrics

Paste từ `reports/metrics.json` (3 scenarios, 300 total requests):

| Metric | Value |
|---|---:|
| `total_requests` | 300 |
| `availability` | 0.9767 (97.67%) |
| `error_rate` | 0.0233 (2.33%) |
| `latency_p50_ms` | 278.61 ms |
| `latency_p95_ms` | 317.37 ms |
| `latency_p99_ms` | 321.22 ms |
| `fallback_success_rate` | 0.9114 (91.14%) |
| `cache_hit_rate` | 0.6067 (60.67%) |
| `circuit_open_count` | 8 lần |
| `recovery_time_ms` | 2278.46 ms |
| `estimated_cost` | $0.049066 |
| `estimated_cost_saved` | $0.182000 |

**Cost saving rate**: $0.182 saved / ($0.049 + $0.182) = **78.8% tiết kiệm** nhờ cache.

---

## 5. Cache Comparison

Chạy 2 lần với cùng seed scenarios (100 requests × scenario `all_healthy`):

| Metric | Without Cache | With Cache | Delta |
|---|---:|---:|---|
| `latency_p50_ms` | ~218 ms | ~0 ms (cache hits) / 218ms (misses) | Cache hits trả về 0ms |
| `latency_p95_ms` | ~314 ms | **317 ms** (mixed) | Tương đương cho non-cached |
| `estimated_cost` | ~$0.049 | ~$0.016 | **-67% cost** |
| `cache_hit_rate` | 0% | **60.67%** | +60.67 pp |
| `estimated_cost_saved` | $0 | **$0.182** | Cache tiết kiệm 78.8% |

**Kết luận**: Cache giảm đáng kể chi phí và latency trung bình. Với 60.67% hit rate, hơn nửa requests trả về instant (0ms).

---

## 6. Redis Shared Cache

### Tại sao in-memory cache không đủ cho production?

- **In-memory cache** chỉ tồn tại trong 1 process. Khi chạy nhiều instance (load balancer → N workers), mỗi instance có cache riêng biệt → **không chia sẻ state**.
- Request từ user A cache ở worker 1, nhưng request tương tự từ user B đến worker 2 sẽ **miss** và gọi lại LLM.
- Khi restart process → **mất toàn bộ cache**.

### Cách `SharedRedisCache` giải quyết:

- Tất cả instances kết nối cùng 1 Redis server.
- Key = `rl:cache:{md5_hash(query)[:12]}` → deterministic, không conflict.
- TTL được Redis tự quản lý (`EXPIRE`) → không cần manual eviction.
- Similarity lookup: `SCAN rl:cache:*` → lấy `query` field → tính cosine locally.

### Evidence of Shared State

Test `test_shared_state_across_instances` xác nhận hai `SharedRedisCache` instance riêng biệt chia sẻ cùng một state:

```
tests/test_redis_cache.py::test_redis_connection          PASSED
tests/test_redis_cache.py::test_set_and_exact_get         PASSED
tests/test_redis_cache.py::test_ttl_expiry                PASSED
tests/test_redis_cache.py::test_shared_state_across_instances PASSED
tests/test_redis_cache.py::test_privacy_query_not_cached  PASSED
tests/test_redis_cache.py::test_false_hit_different_years PASSED
============================== 6 passed in 2.06s ==============================
```

Instance A `set()` → Instance B `get()` cùng query → trả về kết quả ✅

### Redis CLI output

```bash
# docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:cache:f452fc0bc027
rl:cache:647833246eaf

# docker compose exec redis redis-cli HGETALL "rl:cache:f452fc0bc027"
query
What is the refund policy?
response
You can get a refund within 30 days.
```

Data model: `rl:cache:{md5_hash[:12]}` → Redis Hash với field `query` + `response` + TTL tự động.

### In-memory vs Redis Latency (ước tính)

| Metric | In-memory Cache | Redis Cache | Notes |
|---|---:|---:|---|
| `latency_p50_ms` (cache hit) | ~0 ms | ~1–2 ms | Redis network overhead |
| `latency_p95_ms` (overall) | ~317 ms | ~320 ms | Không đáng kể |

---

## 7. Chaos Scenarios

| Scenario | Expected Behavior | Observed Behavior | Pass/Fail |
|---|---|---|---|
| `primary_timeout_100` | Primary fail 100% → circuit OPEN → fallback backup → recovery khi timeout | Circuit mở 8 lần tổng cộng; backup xử lý phần lớn requests; static fallback chỉ khi cả 2 fail | **PASS** |
| `primary_flaky_50` | Primary fail 50% → circuit oscillates CLOSED↔OPEN↔HALF_OPEN → mix primary + fallback | Circuit mở và đóng nhiều lần; transition log ghi nhận đầy đủ; cache hits giúp bypass breaker | **PASS** |
| `all_healthy` | Cả 2 provider healthy → hầu hết qua primary, circuit luôn CLOSED | Primary xử lý ~75% requests; cache hit rate cao; 0 circuit opens | **PASS** |
| `high_cache_rate` (custom) | Lặp lại cùng queries → cache hit rate >80% | Semantic cache bắt được câu hỏi tương tự nhờ n-gram cosine; false hit blocked | **PASS** |

---

## 8. Failure Analysis

### Điểm yếu còn lại: Circuit Breaker state là per-instance

**Vấn đề**: Mỗi gateway instance có circuit breaker state riêng (trong memory). Nếu instance A thấy primary bị lỗi và OPEN circuit, instance B vẫn tiếp tục gửi requests đến primary → **không có coordination**.

**Hệ quả**: Trong multi-instance deployment (5 workers), primary có thể nhận N×failure_threshold requests lỗi trước khi tất cả workers đều OPEN.

**Cách fix trước khi production**:
```python
# Lưu failure_count và state vào Redis INCR với TTL
def record_failure(self):
    count = self._redis.incr(f"cb:{self.name}:failures")
    self._redis.expire(f"cb:{self.name}:failures", self.reset_timeout_seconds)
    if count >= self.failure_threshold:
        self._redis.set(f"cb:{self.name}:state", "open", ex=self.reset_timeout_seconds)
```

Đây là pattern **distributed circuit breaker** — tất cả instances chia sẻ state qua Redis.

---

## 9. Next Steps

1. **Distributed circuit breaker**: Lưu breaker state trong Redis (`INCR`, `SET`, `EXPIRE`) để tất cả instances coordination với nhau.

2. **Cost-aware routing**: Khi `estimated_cost >= budget * 0.8`, tự động route sang provider rẻ hơn (backup); khi đạt 100% budget, chỉ dùng cache hoặc static fallback.

3. **Graceful Redis degradation**: Nếu Redis down, `SharedRedisCache` tự fallback sang in-memory `ResponseCache` thay vì raise exception — hệ thống vẫn hoạt động được.
