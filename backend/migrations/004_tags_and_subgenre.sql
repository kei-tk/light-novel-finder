-- =====================================================================
-- 004_tags_and_subgenre.sql
-- ・series に sub_genre_tags カラムを追加
-- ・タグマスタテーブル tags を追加（タグ名 + 説明 + 種類）
-- ・初期タグデータを投入
-- ・series_engagement ビューを更新
-- =====================================================================

-- 1. series に sub_genre_tags 追加
ALTER TABLE series ADD COLUMN IF NOT EXISTS sub_genre_tags TEXT[];
CREATE INDEX IF NOT EXISTS idx_series_sub_genre ON series USING GIN(sub_genre_tags);

-- 2. タグマスタテーブル
CREATE TABLE IF NOT EXISTS tags (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    tag_type TEXT NOT NULL CHECK (tag_type IN ('atmosphere', 'genre', 'sub_genre', 'theme')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tags_type ON tags(tag_type);

ALTER TABLE tags ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tags read" ON tags;
CREATE POLICY "tags read" ON tags FOR SELECT USING (TRUE);

DROP TRIGGER IF EXISTS trg_tags_updated_at ON tags;
CREATE TRIGGER trg_tags_updated_at
    BEFORE UPDATE ON tags
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 3. 初期タグデータ投入
INSERT INTO tags (name, description, tag_type) VALUES
  -- atmosphere（雰囲気）
  ('胸キュン',         '甘酸っぱい恋愛描写、ドキドキする展開が中心', 'atmosphere'),
  ('ほのぼの',         '穏やかで日常的、安心して読める雰囲気',       'atmosphere'),
  ('ダーク',           '陰鬱・重厚・残酷な空気感',                   'atmosphere'),
  ('シリアス',         '重いテーマや葛藤を真剣に描く',               'atmosphere'),
  ('コメディ',         '笑える、軽快、テンポの良い展開',             'atmosphere'),
  ('泣ける',           '感動・涙を誘う物語',                         'atmosphere'),
  ('熱血',             '熱い友情・努力・勝負が中心',                 'atmosphere'),
  ('鬱',               '読後感が重く、救いの少ない展開',             'atmosphere'),
  ('爽快',             'スカッとする展開・カタルシス重視',           'atmosphere'),
  ('切ない',           '報われない恋愛・別れなど切なさが残る',       'atmosphere'),

  -- genre（ジャンル）
  ('ファンタジー',     '魔法・剣・異世界などが舞台',                 'genre'),
  ('SF',               '未来・宇宙・科学技術がテーマ',               'genre'),
  ('ミステリー',       '謎解き・推理が中心',                         'genre'),
  ('ホラー',           '恐怖・怪異が主軸',                           'genre'),
  ('ラブコメ',         '恋愛とコメディの融合',                       'genre'),
  ('恋愛',             '恋愛関係が物語の主軸',                       'genre'),
  ('アクション',       '戦闘・バトル中心',                           'genre'),
  ('日常系',           '大きな事件のない穏やかな日々を描く',         'genre'),
  ('学園',             '学校・学生生活が舞台',                       'genre'),
  ('歴史',             '過去の時代・史実をベースに描く',             'genre'),
  ('青春',             '若者の成長・友情・恋愛',                     'genre'),

  -- sub_genre（サブジャンル・属性）
  ('ざまぁ',           '理不尽に虐げた相手が後悔・破滅していく爽快展開', 'sub_genre'),
  ('共依存',           '互いに離れられない依存的・歪な関係性',           'sub_genre'),
  ('身長差',           '身長差のあるカップル・キャラ関係',               'sub_genre'),
  ('百合',             '女性同士の関係性・恋愛',                         'sub_genre'),
  ('BL',               '男性同士の関係性・恋愛',                         'sub_genre'),
  ('異世界転生',       '現代から異世界に転生する',                       'sub_genre'),
  ('異世界転移',       '現代から異世界に転移（生まれ変わりではない）',   'sub_genre'),
  ('悪役令嬢',         '乙女ゲームの悪役令嬢に転生する',                 'sub_genre'),
  ('追放',             '主人公がパーティ等から追放される展開',           'sub_genre'),
  ('婚約破棄',         '婚約破棄から物語が始まる',                       'sub_genre'),
  ('ハーレム',         '主人公の周囲に複数の異性が集まる',               'sub_genre'),
  ('逆ハーレム',       '女性主人公の周囲に複数の男性',                   'sub_genre'),
  ('幼なじみ',         '幼なじみキャラ・関係',                           'sub_genre'),
  ('義妹',             '血の繋がらない妹キャラ',                         'sub_genre'),
  ('年の差',           '年齢差のある関係性',                             'sub_genre'),
  ('俺ツエー',         '主人公が圧倒的な強さを持つ',                     'sub_genre'),
  ('チート',           '常識外れの能力で無双する',                       'sub_genre'),
  ('スローライフ',     'のんびりした暮らし・日常を楽しむ',               'sub_genre'),
  ('ダンジョン',       'ダンジョン攻略が中心',                           'sub_genre'),
  ('ゲーム世界',       'ゲームの世界観で展開',                           'sub_genre'),
  ('VRMMO',            'VR型のMMOゲームが舞台',                          'sub_genre'),
  ('ループ',           '時間ループ・繰り返し',                           'sub_genre'),
  ('一目惚れ',         '出会った瞬間に恋に落ちる',                       'sub_genre'),
  ('片想い',           '報われない or 片方だけの想い',                   'sub_genre'),
  ('陰キャ',           '内向的な主人公',                                 'sub_genre'),
  ('ヒロイン強い',     'ヒロインが主人公より強い／優位',                 'sub_genre'),

  -- theme（テーマ）
  ('成長',             '主人公の精神的成長',                             'theme'),
  ('友情',             '仲間との絆',                                     'theme'),
  ('家族',             '家族関係・家族愛',                               'theme'),
  ('復讐',             '復讐が物語の動機',                               'theme'),
  ('正義',             '正義のあり方を問う',                             'theme'),
  ('生死',             '生と死の境界',                                   'theme'),
  ('運命',             '抗えない運命との闘い',                           'theme'),
  ('救済',             '誰かを救うこと',                                 'theme'),
  ('自己肯定',         '自分を受け入れる過程',                           'theme'),
  ('社会風刺',         '社会への批評的視点',                             'theme')
ON CONFLICT (name) DO NOTHING;

-- 4. series_engagement ビューを更新（sub_genre_tags を含める）
DROP VIEW IF EXISTS series_engagement;
CREATE VIEW series_engagement AS
SELECT
    s.id AS series_id,
    s.title,
    s.author,
    s.atmosphere_tags,
    s.genre_tags,
    s.sub_genre_tags,
    s.synopsis,
    s.cover_image_url,
    s.is_trending,
    COUNT(v.id) AS volume_count,
    MAX(v.published_date) AS latest_published_date,
    MIN(v.published_date) AS first_published_date,
    COALESCE(SUM(v.max_tweet_likes), 0) AS total_likes,
    COALESCE(MAX(v.max_tweet_likes), 0) AS max_volume_likes
FROM series s
LEFT JOIN volumes v ON v.series_id = s.id
GROUP BY s.id;
