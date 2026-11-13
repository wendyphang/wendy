-- ============================================================
-- 加竞对专用。只跑这一段,永远不要重跑 schema.sql 的整段。
-- 账号名:纯账号名,不要 @,不要链接。重复跑不会报错。
-- ============================================================
insert into competitors (username, tracker, active) values
  ('新竞对1', 'IG', true),
  ('新竞对2', 'IG', true),
  ('新竞对3', 'IG', true)
on conflict do nothing;

-- 想停掉某个竞对(保留数据,只是不再抓):
-- update competitors set active = false where lower(username) = lower('要停的账号名');

-- 看现在在追踪哪些:
-- select username, tracker, active, notes from competitors order by active desc, username;
