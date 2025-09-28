-- Mirrors your sample table, tuned for SQLite types
CREATE TABLE IF NOT EXISTS Zoho_Channels (
    Id INTEGER PRIMARY KEY AUTOINCREMENT,
    Account_Id TEXT DEFAULT NULL,
    Access_Token TEXT DEFAULT NULL,
    Refresh_Token TEXT DEFAULT NULL,
    AT_Expiry INTEGER DEFAULT NULL,
    RT_Expiry INTEGER DEFAULT NULL,
    Created_Time INTEGER NOT NULL,
    Modified_Time INTEGER NOT NULL,
    Domain TEXT DEFAULT NULL,
    User_Id INTEGER DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_Zoho_Channels_UserId ON Zoho_Channels(User_Id);
