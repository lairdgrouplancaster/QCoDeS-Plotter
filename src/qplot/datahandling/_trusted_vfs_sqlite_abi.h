/*
 * Minimal SQLite 3.53.4 loadable-extension ABI used by qPlot's trusted VFS.
 *
 * SQLite's extension API is an append-only function-pointer table.  This
 * header deliberately declares only the stable prefix through the URI helper
 * functions used by the VFS.  Keep it pinned in lockstep with APSW 3.53.4.0;
 * the extension rejects every other SQLite runtime before registering.
 *
 * The declarations are derived from SQLite 3.53.4's public-domain sqlite3.h
 * and sqlite3ext.h files.  No SQLite implementation code is bundled here.
 */
#ifndef QPLOT_TRUSTED_VFS_SQLITE_ABI_H
#define QPLOT_TRUSTED_VFS_SQLITE_ABI_H

#include <stdarg.h>
#include <stddef.h>
#define QPLOT_TRUSTED_SQLITE_VERSION_NUMBER 3053004

#ifdef SQLITE_INT64_TYPE
typedef SQLITE_INT64_TYPE sqlite_int64;
# ifdef SQLITE_UINT64_TYPE
typedef SQLITE_UINT64_TYPE sqlite_uint64;
# else
typedef unsigned SQLITE_INT64_TYPE sqlite_uint64;
# endif
#elif defined(_MSC_VER) || defined(__BORLANDC__)
typedef __int64 sqlite_int64;
typedef unsigned __int64 sqlite_uint64;
#else
typedef long long int sqlite_int64;
typedef unsigned long long int sqlite_uint64;
#endif
typedef sqlite_int64 sqlite3_int64;
typedef sqlite_uint64 sqlite3_uint64;

typedef struct sqlite3 sqlite3;
typedef struct sqlite3_backup sqlite3_backup;
typedef struct sqlite3_blob sqlite3_blob;
typedef struct sqlite3_context sqlite3_context;
typedef struct sqlite3_file sqlite3_file;
typedef struct sqlite3_index_info sqlite3_index_info;
typedef struct sqlite3_io_methods sqlite3_io_methods;
typedef struct sqlite3_module sqlite3_module;
typedef struct sqlite3_mutex sqlite3_mutex;
typedef struct sqlite3_stmt sqlite3_stmt;
typedef struct sqlite3_value sqlite3_value;
typedef struct sqlite3_vfs sqlite3_vfs;
typedef const char *sqlite3_filename;
typedef void (*sqlite3_syscall_ptr)(void);
typedef int (*sqlite3_callback)(void *, int, char **, char **);

struct sqlite3_file {
  const sqlite3_io_methods *pMethods;
};

struct sqlite3_io_methods {
  int iVersion;
  int (*xClose)(sqlite3_file *);
  int (*xRead)(sqlite3_file *, void *, int, sqlite3_int64);
  int (*xWrite)(sqlite3_file *, const void *, int, sqlite3_int64);
  int (*xTruncate)(sqlite3_file *, sqlite3_int64);
  int (*xSync)(sqlite3_file *, int);
  int (*xFileSize)(sqlite3_file *, sqlite3_int64 *);
  int (*xLock)(sqlite3_file *, int);
  int (*xUnlock)(sqlite3_file *, int);
  int (*xCheckReservedLock)(sqlite3_file *, int *);
  int (*xFileControl)(sqlite3_file *, int, void *);
  int (*xSectorSize)(sqlite3_file *);
  int (*xDeviceCharacteristics)(sqlite3_file *);
  int (*xShmMap)(sqlite3_file *, int, int, int, void volatile **);
  int (*xShmLock)(sqlite3_file *, int, int, int);
  void (*xShmBarrier)(sqlite3_file *);
  int (*xShmUnmap)(sqlite3_file *, int);
  int (*xFetch)(sqlite3_file *, sqlite3_int64, int, void **);
  int (*xUnfetch)(sqlite3_file *, sqlite3_int64, void *);
};

struct sqlite3_vfs {
  int iVersion;
  int szOsFile;
  int mxPathname;
  sqlite3_vfs *pNext;
  const char *zName;
  void *pAppData;
  int (*xOpen)(sqlite3_vfs *, sqlite3_filename, sqlite3_file *, int, int *);
  int (*xDelete)(sqlite3_vfs *, const char *, int);
  int (*xAccess)(sqlite3_vfs *, const char *, int, int *);
  int (*xFullPathname)(sqlite3_vfs *, const char *, int, char *);
  void *(*xDlOpen)(sqlite3_vfs *, const char *);
  void (*xDlError)(sqlite3_vfs *, int, char *);
  void (*(*xDlSym)(sqlite3_vfs *, void *, const char *))(void);
  void (*xDlClose)(sqlite3_vfs *, void *);
  int (*xRandomness)(sqlite3_vfs *, int, char *);
  int (*xSleep)(sqlite3_vfs *, int);
  int (*xCurrentTime)(sqlite3_vfs *, double *);
  int (*xGetLastError)(sqlite3_vfs *, int, char *);
  int (*xCurrentTimeInt64)(sqlite3_vfs *, sqlite3_int64 *);
  int (*xSetSystemCall)(sqlite3_vfs *, const char *, sqlite3_syscall_ptr);
  sqlite3_syscall_ptr (*xGetSystemCall)(sqlite3_vfs *, const char *);
  const char *(*xNextSystemCall)(sqlite3_vfs *, const char *);
};

/* Exact append-only sqlite3_api_routines prefix through SQLite 3.7.16's URI
 * helpers.  Field order is the loadable-extension ABI. */
typedef struct sqlite3_api_routines {
  void *(*aggregate_context)(sqlite3_context *, int);
  int (*aggregate_count)(sqlite3_context *);
  int (*bind_blob)(sqlite3_stmt *, int, const void *, int, void (*)(void *));
  int (*bind_double)(sqlite3_stmt *, int, double);
  int (*bind_int)(sqlite3_stmt *, int, int);
  int (*bind_int64)(sqlite3_stmt *, int, sqlite_int64);
  int (*bind_null)(sqlite3_stmt *, int);
  int (*bind_parameter_count)(sqlite3_stmt *);
  int (*bind_parameter_index)(sqlite3_stmt *, const char *);
  const char *(*bind_parameter_name)(sqlite3_stmt *, int);
  int (*bind_text)(sqlite3_stmt *, int, const char *, int, void (*)(void *));
  int (*bind_text16)(sqlite3_stmt *, int, const void *, int, void (*)(void *));
  int (*bind_value)(sqlite3_stmt *, int, const sqlite3_value *);
  int (*busy_handler)(sqlite3 *, int (*)(void *, int), void *);
  int (*busy_timeout)(sqlite3 *, int);
  int (*changes)(sqlite3 *);
  int (*close)(sqlite3 *);
  int (*collation_needed)(sqlite3 *, void *, void (*)(void *, sqlite3 *, int,
                                                       const char *));
  int (*collation_needed16)(sqlite3 *, void *, void (*)(void *, sqlite3 *, int,
                                                         const void *));
  const void *(*column_blob)(sqlite3_stmt *, int);
  int (*column_bytes)(sqlite3_stmt *, int);
  int (*column_bytes16)(sqlite3_stmt *, int);
  int (*column_count)(sqlite3_stmt *);
  const char *(*column_database_name)(sqlite3_stmt *, int);
  const void *(*column_database_name16)(sqlite3_stmt *, int);
  const char *(*column_decltype)(sqlite3_stmt *, int);
  const void *(*column_decltype16)(sqlite3_stmt *, int);
  double (*column_double)(sqlite3_stmt *, int);
  int (*column_int)(sqlite3_stmt *, int);
  sqlite_int64 (*column_int64)(sqlite3_stmt *, int);
  const char *(*column_name)(sqlite3_stmt *, int);
  const void *(*column_name16)(sqlite3_stmt *, int);
  const char *(*column_origin_name)(sqlite3_stmt *, int);
  const void *(*column_origin_name16)(sqlite3_stmt *, int);
  const char *(*column_table_name)(sqlite3_stmt *, int);
  const void *(*column_table_name16)(sqlite3_stmt *, int);
  const unsigned char *(*column_text)(sqlite3_stmt *, int);
  const void *(*column_text16)(sqlite3_stmt *, int);
  int (*column_type)(sqlite3_stmt *, int);
  sqlite3_value *(*column_value)(sqlite3_stmt *, int);
  void *(*commit_hook)(sqlite3 *, int (*)(void *), void *);
  int (*complete)(const char *);
  int (*complete16)(const void *);
  int (*create_collation)(sqlite3 *, const char *, int, void *,
                          int (*)(void *, int, const void *, int,
                                  const void *));
  int (*create_collation16)(sqlite3 *, const void *, int, void *,
                            int (*)(void *, int, const void *, int,
                                    const void *));
  int (*create_function)(sqlite3 *, const char *, int, int, void *,
                         void (*)(sqlite3_context *, int, sqlite3_value **),
                         void (*)(sqlite3_context *, int, sqlite3_value **),
                         void (*)(sqlite3_context *));
  int (*create_function16)(sqlite3 *, const void *, int, int, void *,
                           void (*)(sqlite3_context *, int, sqlite3_value **),
                           void (*)(sqlite3_context *, int, sqlite3_value **),
                           void (*)(sqlite3_context *));
  int (*create_module)(sqlite3 *, const char *, const sqlite3_module *, void *);
  int (*data_count)(sqlite3_stmt *);
  sqlite3 *(*db_handle)(sqlite3_stmt *);
  int (*declare_vtab)(sqlite3 *, const char *);
  int (*enable_shared_cache)(int);
  int (*errcode)(sqlite3 *);
  const char *(*errmsg)(sqlite3 *);
  const void *(*errmsg16)(sqlite3 *);
  int (*exec)(sqlite3 *, const char *, sqlite3_callback, void *, char **);
  int (*expired)(sqlite3_stmt *);
  int (*finalize)(sqlite3_stmt *);
  void (*free)(void *);
  void (*free_table)(char **);
  int (*get_autocommit)(sqlite3 *);
  void *(*get_auxdata)(sqlite3_context *, int);
  int (*get_table)(sqlite3 *, const char *, char ***, int *, int *, char **);
  int (*global_recover)(void);
  void (*interruptx)(sqlite3 *);
  sqlite_int64 (*last_insert_rowid)(sqlite3 *);
  const char *(*libversion)(void);
  int (*libversion_number)(void);
  void *(*malloc)(int);
  char *(*mprintf)(const char *, ...);
  int (*open)(const char *, sqlite3 **);
  int (*open16)(const void *, sqlite3 **);
  int (*prepare)(sqlite3 *, const char *, int, sqlite3_stmt **, const char **);
  int (*prepare16)(sqlite3 *, const void *, int, sqlite3_stmt **,
                   const void **);
  void *(*profile)(sqlite3 *, void (*)(void *, const char *, sqlite_uint64),
                   void *);
  void (*progress_handler)(sqlite3 *, int, int (*)(void *), void *);
  void *(*realloc)(void *, int);
  int (*reset)(sqlite3_stmt *);
  void (*result_blob)(sqlite3_context *, const void *, int, void (*)(void *));
  void (*result_double)(sqlite3_context *, double);
  void (*result_error)(sqlite3_context *, const char *, int);
  void (*result_error16)(sqlite3_context *, const void *, int);
  void (*result_int)(sqlite3_context *, int);
  void (*result_int64)(sqlite3_context *, sqlite_int64);
  void (*result_null)(sqlite3_context *);
  void (*result_text)(sqlite3_context *, const char *, int, void (*)(void *));
  void (*result_text16)(sqlite3_context *, const void *, int, void (*)(void *));
  void (*result_text16be)(sqlite3_context *, const void *, int,
                          void (*)(void *));
  void (*result_text16le)(sqlite3_context *, const void *, int,
                          void (*)(void *));
  void (*result_value)(sqlite3_context *, sqlite3_value *);
  void *(*rollback_hook)(sqlite3 *, void (*)(void *), void *);
  int (*set_authorizer)(sqlite3 *, int (*)(void *, int, const char *,
                                           const char *, const char *,
                                           const char *),
                        void *);
  void (*set_auxdata)(sqlite3_context *, int, void *, void (*)(void *));
  char *(*xsnprintf)(int, char *, const char *, ...);
  int (*step)(sqlite3_stmt *);
  int (*table_column_metadata)(sqlite3 *, const char *, const char *,
                               const char *, const char **, const char **, int *,
                               int *, int *);
  void (*thread_cleanup)(void);
  int (*total_changes)(sqlite3 *);
  void *(*trace)(sqlite3 *, void (*)(void *, const char *), void *);
  int (*transfer_bindings)(sqlite3_stmt *, sqlite3_stmt *);
  void *(*update_hook)(sqlite3 *,
                       void (*)(void *, int, const char *, const char *,
                                sqlite_int64),
                       void *);
  void *(*user_data)(sqlite3_context *);
  const void *(*value_blob)(sqlite3_value *);
  int (*value_bytes)(sqlite3_value *);
  int (*value_bytes16)(sqlite3_value *);
  double (*value_double)(sqlite3_value *);
  int (*value_int)(sqlite3_value *);
  sqlite_int64 (*value_int64)(sqlite3_value *);
  int (*value_numeric_type)(sqlite3_value *);
  const unsigned char *(*value_text)(sqlite3_value *);
  const void *(*value_text16)(sqlite3_value *);
  const void *(*value_text16be)(sqlite3_value *);
  const void *(*value_text16le)(sqlite3_value *);
  int (*value_type)(sqlite3_value *);
  char *(*vmprintf)(const char *, va_list);
  int (*overload_function)(sqlite3 *, const char *, int);
  int (*prepare_v2)(sqlite3 *, const char *, int, sqlite3_stmt **,
                    const char **);
  int (*prepare16_v2)(sqlite3 *, const void *, int, sqlite3_stmt **,
                      const void **);
  int (*clear_bindings)(sqlite3_stmt *);
  int (*create_module_v2)(sqlite3 *, const char *, const sqlite3_module *,
                          void *, void (*)(void *));
  int (*bind_zeroblob)(sqlite3_stmt *, int, int);
  int (*blob_bytes)(sqlite3_blob *);
  int (*blob_close)(sqlite3_blob *);
  int (*blob_open)(sqlite3 *, const char *, const char *, const char *,
                   sqlite3_int64, int, sqlite3_blob **);
  int (*blob_read)(sqlite3_blob *, void *, int, int);
  int (*blob_write)(sqlite3_blob *, const void *, int, int);
  int (*create_collation_v2)(sqlite3 *, const char *, int, void *,
                             int (*)(void *, int, const void *, int,
                                     const void *),
                             void (*)(void *));
  int (*file_control)(sqlite3 *, const char *, int, void *);
  sqlite3_int64 (*memory_highwater)(int);
  sqlite3_int64 (*memory_used)(void);
  sqlite3_mutex *(*mutex_alloc)(int);
  void (*mutex_enter)(sqlite3_mutex *);
  void (*mutex_free)(sqlite3_mutex *);
  void (*mutex_leave)(sqlite3_mutex *);
  int (*mutex_try)(sqlite3_mutex *);
  int (*open_v2)(const char *, sqlite3 **, int, const char *);
  int (*release_memory)(int);
  void (*result_error_nomem)(sqlite3_context *);
  void (*result_error_toobig)(sqlite3_context *);
  int (*sleep)(int);
  void (*soft_heap_limit)(int);
  sqlite3_vfs *(*vfs_find)(const char *);
  int (*vfs_register)(sqlite3_vfs *, int);
  int (*vfs_unregister)(sqlite3_vfs *);
  int (*xthreadsafe)(void);
  void (*result_zeroblob)(sqlite3_context *, int);
  void (*result_error_code)(sqlite3_context *, int);
  int (*test_control)(int, ...);
  void (*randomness)(int, void *);
  sqlite3 *(*context_db_handle)(sqlite3_context *);
  int (*extended_result_codes)(sqlite3 *, int);
  int (*limit)(sqlite3 *, int, int);
  sqlite3_stmt *(*next_stmt)(sqlite3 *, sqlite3_stmt *);
  const char *(*sql)(sqlite3_stmt *);
  int (*status)(int, int *, int *, int);
  int (*backup_finish)(sqlite3_backup *);
  sqlite3_backup *(*backup_init)(sqlite3 *, const char *, sqlite3 *,
                                 const char *);
  int (*backup_pagecount)(sqlite3_backup *);
  int (*backup_remaining)(sqlite3_backup *);
  int (*backup_step)(sqlite3_backup *, int);
  const char *(*compileoption_get)(int);
  int (*compileoption_used)(const char *);
  int (*create_function_v2)(sqlite3 *, const char *, int, int, void *,
                            void (*)(sqlite3_context *, int, sqlite3_value **),
                            void (*)(sqlite3_context *, int, sqlite3_value **),
                            void (*)(sqlite3_context *), void (*)(void *));
  int (*db_config)(sqlite3 *, int, ...);
  sqlite3_mutex *(*db_mutex)(sqlite3 *);
  int (*db_status)(sqlite3 *, int, int *, int *, int);
  int (*extended_errcode)(sqlite3 *);
  void (*log)(int, const char *, ...);
  sqlite3_int64 (*soft_heap_limit64)(sqlite3_int64);
  const char *(*sourceid)(void);
  int (*stmt_status)(sqlite3_stmt *, int, int);
  int (*strnicmp)(const char *, const char *, int);
  int (*unlock_notify)(sqlite3 *, void (*)(void **, int), void *);
  int (*wal_autocheckpoint)(sqlite3 *, int);
  int (*wal_checkpoint)(sqlite3 *, const char *);
  void *(*wal_hook)(sqlite3 *, int (*)(void *, sqlite3 *, const char *, int),
                    void *);
  int (*blob_reopen)(sqlite3_blob *, sqlite3_int64);
  int (*vtab_config)(sqlite3 *, int, ...);
  int (*vtab_on_conflict)(sqlite3 *);
  int (*close_v2)(sqlite3 *);
  const char *(*db_filename)(sqlite3 *, const char *);
  int (*db_readonly)(sqlite3 *, const char *);
  int (*db_release_memory)(sqlite3 *);
  const char *(*errstr)(int);
  int (*stmt_busy)(sqlite3_stmt *);
  int (*stmt_readonly)(sqlite3_stmt *);
  int (*stricmp)(const char *, const char *);
  int (*uri_boolean)(const char *, const char *, int);
  sqlite3_int64 (*uri_int64)(const char *, const char *, sqlite3_int64);
  const char *(*uri_parameter)(const char *, const char *);
} sqlite3_api_routines;

#define SQLITE_OK 0
#define SQLITE_ERROR 1
#define SQLITE_BUSY 5
#define SQLITE_NOMEM 7
#define SQLITE_READONLY 8
#define SQLITE_INTERRUPT 9
#define SQLITE_IOERR 10
#define SQLITE_CORRUPT 11
#define SQLITE_NOTFOUND 12
#define SQLITE_CANTOPEN 14
#define SQLITE_MISUSE 21
#define SQLITE_READONLY_CANTINIT (SQLITE_READONLY | (5 << 8))
#define SQLITE_CANTOPEN_SYMLINK (SQLITE_CANTOPEN | (6 << 8))
#define SQLITE_IOERR_FSTAT (SQLITE_IOERR | (7 << 8))
#define SQLITE_IOERR_SHMMAP (SQLITE_IOERR | (21 << 8))
#define SQLITE_OK_LOAD_PERMANENTLY (SQLITE_OK | (1 << 8))

#define SQLITE_OPEN_READONLY 0x00000001
#define SQLITE_OPEN_READWRITE 0x00000002
#define SQLITE_OPEN_CREATE 0x00000004
#define SQLITE_OPEN_DELETEONCLOSE 0x00000008
#define SQLITE_OPEN_EXCLUSIVE 0x00000010
#define SQLITE_OPEN_URI 0x00000040
#define SQLITE_OPEN_NOFOLLOW 0x01000000
#define SQLITE_OPEN_MAIN_DB 0x00000100
#define SQLITE_OPEN_TEMP_DB 0x00000200
#define SQLITE_OPEN_TRANSIENT_DB 0x00000400
#define SQLITE_OPEN_MAIN_JOURNAL 0x00000800
#define SQLITE_OPEN_TEMP_JOURNAL 0x00001000
#define SQLITE_OPEN_SUBJOURNAL 0x00002000
#define SQLITE_OPEN_SUPER_JOURNAL 0x00004000
#define SQLITE_OPEN_WAL 0x00080000

#define SQLITE_FCNTL_MMAP_SIZE 18
#define SQLITE_FCNTL_HAS_MOVED 20
#define SQLITE_FCNTL_WIN32_GET_HANDLE 29
#define SQLITE_UTF8 1
#define SQLITE_DETERMINISTIC 0x000000800
#define SQLITE_DIRECTONLY 0x000080000
#define SQLITE_INNOCUOUS 0x000200000
#define SQLITE_MUTEX_FAST 0
#define SQLITE_MUTEX_STATIC_MASTER 2
#define SQLITE_MUTEX_STATIC_VFS2 12

#define QPLOT_SQLITE_TRANSIENT ((void (*)(void *))-1)

#endif
