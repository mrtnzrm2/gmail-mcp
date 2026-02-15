from slowapi import Limiter

from .ratelimit_key import rate_limit_key

limiter = Limiter(key_func=rate_limit_key)
