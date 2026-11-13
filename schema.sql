-- ============================================================
-- 爆款雷达 · Supabase 建表
--
-- ✅ 这版可以安全重复跑:不会删任何数据。
--    (旧版第一行是 drop table,重跑一次 = 所有帖子/转录/AI拆解/状态全没)
--
-- 在 Supabase → SQL Editor 整段粘贴 → Run,看到 Success 即可。
-- ============================================================

-- ---------- 爆款帖子 ----------
create table if not exists posts (
  id              bigint generated always as identity primary key,
  post_id         text unique not null,
  tracker         text not null default 'IG',
  competitor      text,
  caption         text,
  transcript      text,                            -- 视频口播稿(自动转录)
  ai_breakdown    text,                            -- AI 拆解(自动生成)
  my_script       text,                            -- 我的改写口播稿(AI 按 brand_voice.md 改写,可直接拍)
  post_type       text,
  likes           integer default 0,
  comments        integer default 0,
  followers       integer default 0,
  engagement_rate numeric generated always as
      ((likes + comments)::numeric / nullif(followers, 0)) stored,
  viral_score     numeric generated always as
      (round((greatest(likes, 0) + comments * 3)::numeric / nullif(followers, 0) * 100, 2)) stored,
  post_date       date,
  post_url        text,
  thumbnail_url   text,
  video_url       text,
  hashtags        text,
  is_video        boolean default false,
  status          text default '未处理',            -- 未处理/拍摄中/已处理/跳过
  last_synced     date,
  created_at      timestamptz default now()
);

-- ---------- 竞对名单(自助管理:加行=追踪,取消active=停) ----------
create table if not exists competitors (
  id         bigint generated always as identity primary key,
  username   text not null,
  tracker    text not null default 'IG',
  active     boolean default true,
  notes      text,
  created_at timestamptz default now()
);

-- ============================================================
-- 数据清洗 + 约束(老版本升级上来的会用到,新装的跑了也没副作用)
-- ============================================================

-- 竞对账号名规范化:去掉 @ 和前后空格
update competitors
   set username = ltrim(btrim(username), '@')
 where username <> ltrim(btrim(username), '@');

-- 去掉重复竞对(重复 = sync 时同一个账号抓两次 = Apify 双倍花钱)
delete from competitors a
 using competitors b
 where a.id > b.id
   and lower(a.username) = lower(b.username)
   and a.tracker = b.tracker;

-- 以后插不进重复的了
create unique index if not exists competitors_username_uniq
    on competitors (lower(username), tracker);

-- 老版本升级:补上「我的稿」这一列(新装的已经有了,重跑没副作用)
alter table posts add column if not exists my_script text;

-- status 白名单:公开 key 能改这一列,不锁死的话任何人可以往里塞任意长文本
update posts set status = '未处理'
 where status is null or status not in ('未处理', '拍摄中', '已处理', '跳过');

alter table posts drop constraint if exists posts_status_chk;
alter table posts add  constraint posts_status_chk
      check (status in ('未处理', '拍摄中', '已处理', '跳过'));

create index if not exists posts_status_idx     on posts (status);
create index if not exists posts_score_idx      on posts (viral_score desc nulls last);
create index if not exists posts_competitor_idx on posts (competitor);
create index if not exists posts_synced_idx     on posts (last_synced);

-- ============================================================
-- 安全锁(关键!):浏览器的公开 key 只能【读帖子】+【改 status 一列】。
-- 必须先 REVOKE ALL —— Supabase 默认给公开 key 全部权限,不撤掉的话
-- 任何拿到网址的人都能删光你的数据。
-- ============================================================
alter table posts       enable row level security;
alter table competitors enable row level security;

revoke all on posts       from anon;
revoke all on competitors from anon;

grant select on posts to anon;
grant update (status) on posts to anon;

drop policy if exists "anon read posts"    on posts;
drop policy if exists "anon update status" on posts;
create policy "anon read posts"    on posts for select to anon using (true);
create policy "anon update status" on posts for update to anon using (true) with check (true);

-- ============================================================
-- ↓↓↓ 全篇只有这 5 行要改:换成你要追踪的竞对 IG 账号 ↓↓↓
--
--   只要账号名,不要 @,不要链接
--   ✅ nike        ❌ @nike        ❌ instagram.com/nike
--
--   想追踪更多?复制其中一行、改账号名即可(建议 8-15 个)
--   重复跑不会报错,也不会产生重复竞对
-- ============================================================
insert into competitors (username, tracker, active) values
  ('换成竞对1', 'IG', true),
  ('换成竞对2', 'IG', true),
  ('换成竞对3', 'IG', true),
  ('换成竞对4', 'IG', true),
  ('换成竞对5', 'IG', true)
on conflict do nothing;
