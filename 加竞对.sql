-- ============================================================
-- 加竞对专用。只跑这一段,永远不要重跑 schema.sql 的整段。
-- 重复跑不会报错。第二格 = 平台:
--   'IG'  Instagram  → 纯账号名,不要 @
--   'FB'  Facebook   → 专页名或专页链接(只能是 Page,个人号抓不到)
--   'XHS' 小红书      → 主页链接 https://www.xiaohongshu.com/user/profile/xxxx
-- ============================================================
insert into competitors (username, tracker, active) values
  ('新竞对1', 'IG', true),
  ('https://www.facebook.com/新专页', 'FB', true),
  ('https://www.xiaohongshu.com/user/profile/新用户ID', 'XHS', true)
on conflict do nothing;

-- 想停掉某个竞对(保留数据,只是不再抓):
-- update competitors set active = false where lower(username) = lower('要停的账号名') and tracker = 'IG';

-- 看现在在追踪哪些:
-- select tracker, username, active, notes from competitors order by tracker, active desc, username;
