# Backend Architecture Rules

1. **Database Access**: Always use `pool = get_pool()`.
2. **Distributed Caching**: Use `services.cache_service` for caching and call `emit_cache_invalidation` on writes.
3. **Batch Operations**: Use `UNNEST` for multi-row inserts and updates.
4. **Clean Codebase**: Always clean up temporary test scripts before finishing tasks.
