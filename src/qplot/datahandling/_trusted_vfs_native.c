/*
 * qPlot's native, fail-closed SQLite read-only VFS.
 *
 * This file has two entry points in the same shared library:
 *
 *   - PyInit__trusted_vfs_native makes it an abi3 CPython module, so Python
 *     can locate the installed shared object without linking to SQLite.
 *   - sqlite3_qplot_trusted_vfs_init is SQLite's loadable-extension entry
 *     point.  It receives the function table belonging to APSW's private,
 *     pinned SQLite and registers the VFS into that exact SQLite instance.
 *
 * No sqlite3_* symbol is linked directly.  Keep the accompanying ABI header
 * and all policy assumptions pinned to SQLite 3.53.4 / APSW 3.53.4.0.
 */

#define PY_SSIZE_T_CLEAN
#ifndef Py_LIMITED_API
#define Py_LIMITED_API 0x030b0000
#endif
#include <Python.h>

#include "_trusted_vfs_sqlite_abi.h"

#include <ctype.h>
#include <limits.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

#ifdef _WIN32
# define WIN32_LEAN_AND_MEAN
# include <windows.h>
#else
# include <errno.h>
# include <fcntl.h>
# include <sys/stat.h>
# include <sys/types.h>
# include <unistd.h>
# ifndef O_CLOEXEC
#  define O_CLOEXEC 0
# endif
# ifndef O_NOFOLLOW
#  error "qPlot's trusted VFS requires O_NOFOLLOW"
# endif
# ifdef __APPLE__
#  include <sys/mount.h>
# elif defined(__linux__)
#  include <sys/vfs.h>
# endif
#endif

#if UINTPTR_MAX != 0xffffffffffffffffULL
# error "qPlot's trusted VFS currently supports only 64-bit processes"
#endif

#ifdef _WIN32
# define QP_EXPORT __declspec(dllexport)
# define QP_PATH_SEPARATOR '\\'
#else
# define QP_EXPORT __attribute__((visibility("default")))
# define QP_PATH_SEPARATOR '/'
#endif

#define QP_VFS_NAME "qplot-trusted-live-v2"
#define QP_SQLITE_SOURCE_ID \
  "2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab397887" \
  "9485813aaca6f641c7b33e4e09459bcc"
#define QP_TOKEN_CAP 129
#define QP_PATH_CAP 32768
#define QP_JSON_CAP 4096
#define QP_FAILURE_TEXT_CAP 48
#define QP_WAL_HEADER_SIZE 32
#define QP_WAL_MAGIC 0x377f0682U
#define QP_WAL_FORMAT_VERSION 3007000U

/* File-control numbers are pinned to SQLite 3.53.4. */
#define QP_FCNTL_LOCKSTATE 1
#define QP_FCNTL_SIZE_HINT 5
#define QP_FCNTL_CHUNK_SIZE 6
#define QP_FCNTL_SYNC_OMITTED 8
#define QP_FCNTL_PERSIST_WAL 10
#define QP_FCNTL_POWERSAFE_OVERWRITE 13
#define QP_FCNTL_OVERWRITE 11
#define QP_FCNTL_BEGIN_ATOMIC_WRITE 31
#define QP_FCNTL_COMMIT_ATOMIC_WRITE 32
#define QP_FCNTL_ROLLBACK_ATOMIC_WRITE 33
#define QP_FCNTL_CKPT_DONE 37
#define QP_FCNTL_RESERVE_BYTES 38
#define QP_FCNTL_CKPT_START 39
#define QP_FCNTL_CKSM_FILE 41
#define QP_FCNTL_RESET_CACHE 42
#define QP_FCNTL_NULL_IO 43

typedef unsigned long long QpU64;

enum QpArtifact {
  QP_ARTIFACT_NONE = 0,
  QP_ARTIFACT_MAIN = 1,
  QP_ARTIFACT_WAL = 2,
  QP_ARTIFACT_SHM = 3,
  QP_ARTIFACT_JOURNAL = 4,
  QP_ARTIFACT_TEMP = 5
};

enum QpFailureKind {
  QP_FAILURE_NONE = 0,
  QP_FAILURE_SOURCE_CHANGED = 1,
  QP_FAILURE_UNSUPPORTED = 2,
  QP_FAILURE_POLICY = 3,
  QP_FAILURE_IO = 4
};

/* Result of inspecting the current pathname.  Keep this separate from the
 * retained-handle identity so SourceChanged is emitted only when the name is
 * demonstrably missing or resolves to a different stable identity. */
enum QpPathIdentityState {
  QP_PATH_IDENTITY_ALIAS = -3,
  QP_PATH_IDENTITY_IO = -2,
  QP_PATH_IDENTITY_UNSAFE = -1,
  QP_PATH_IDENTITY_MISSING = 0,
  QP_PATH_IDENTITY_PRESENT = 1
};

static int qp_failure_kind_for_path_state(int state,
                                          int present_failure_kind) {
  if (state == QP_PATH_IDENTITY_MISSING) return QP_FAILURE_SOURCE_CHANGED;
  if (state == QP_PATH_IDENTITY_ALIAS ||
      state == QP_PATH_IDENTITY_UNSAFE) return QP_FAILURE_UNSUPPORTED;
  if (state == QP_PATH_IDENTITY_IO) return QP_FAILURE_IO;
  return present_failure_kind;
}

typedef struct QpAudit {
  QpU64 source_open_readonly;
  QpU64 source_open_readwrite;
  QpU64 source_open_create;
  QpU64 source_open_delete_on_close;
  QpU64 source_open_flags_stripped;
  QpU64 source_read;
  QpU64 source_read_bytes;
  QpU64 source_write;
  QpU64 source_truncate;
  QpU64 source_sync;
  QpU64 source_delete;
  QpU64 source_fetch;
  QpU64 source_writable_map;
  QpU64 shm_map_readonly;
  QpU64 shm_map_writable;
  QpU64 shm_map_extend;
  QpU64 shm_map_rejected;
  QpU64 shm_lock;
  QpU64 shm_unmap_delete_requested;
  QpU64 temp_redirect;
  QpU64 temp_write;
  QpU64 temp_write_bytes;
  QpU64 temp_delete;
  QpU64 stale_callback_rejected;
  QpU64 identity_verified;
  QpU64 identity_rejected;
  QpU64 proof_open;
  QpU64 proof_close;
  QpU64 proof_close_error;
  QpU64 proof_active;
  QpU64 proof_peak;
  QpU64 shm_unmap;
  QpU64 shm_unmap_error;
  QpU64 shm_unmap_delete_forwarded;
  QpU64 partial_open_cleanup;
  QpU64 base_close_error;
} QpAudit;

typedef struct QpState {
  int configured;
  int active_files;
  int session_claimed;
  QpU64 generation;
  QpU64 temp_sequence;
  char token[QP_TOKEN_CAP];
  char main_path[QP_PATH_CAP];
  char temp_path[QP_PATH_CAP];
  char race_artifact[16];
  char cleanup_fault[16];
  int cleanup_fault_fired;
  int configured_wal_present;
  QpU64 configured_wal_a;
  QpU64 configured_wal_b;
  int configured_shm_present;
  QpU64 configured_shm_a;
  QpU64 configured_shm_b;
  int expected_wal_present;
  int expected_wal_pending;
  QpU64 expected_wal_a;
  QpU64 expected_wal_b;
  int expected_shm_present;
  int expected_shm_pending;
  QpU64 expected_shm_a;
  QpU64 expected_shm_b;
  int cleanup_failed;
  int failure_kind;
  int failure_artifact;
  int failure_sqlite_code;
  QpU64 failure_sequence;
  char failure_operation[QP_FAILURE_TEXT_CAP];
  QpAudit audit;
} QpState;

enum QpFileKind {
  QP_FILE_NONE = 0,
  QP_FILE_SOURCE = 1,
  QP_FILE_TEMP = 2
};

#ifdef _WIN32
typedef struct QpIdentity {
  DWORD volume_serial;
  QpU64 file_index;
  int valid;
} QpIdentity;

typedef LONG(NTAPI *QpNtQueryObject)(HANDLE, int, PVOID, ULONG, PULONG);
typedef struct QpPublicObjectBasicInformation {
  ULONG attributes;
  ACCESS_MASK granted_access;
  ULONG handle_count;
  ULONG pointer_count;
  ULONG reserved[10];
} QpPublicObjectBasicInformation;

typedef struct QpPinnedWinShmNode QpPinnedWinShmNode;
typedef struct QpPinnedWinShm QpPinnedWinShm;
typedef struct QpPinnedWinFile {
  const sqlite3_io_methods *pMethod;
  sqlite3_vfs *pVfs;
  HANDLE h;
  unsigned char locktype;
  short sharedLockByte;
  unsigned char ctrlFlags;
  DWORD lastErrno;
  QpPinnedWinShm *pShm;
} QpPinnedWinFile;
struct QpPinnedWinShmNode {
  sqlite3_mutex *mutex;
  char *zFilename;
  HANDLE hSharedShm;
  int bUseSharedLockHandle;
  int isUnlocked;
  int isReadonly;
};
struct QpPinnedWinShm {
  QpPinnedWinShmNode *pShmNode;
  unsigned short sharedMask;
  unsigned short exclMask;
  HANDLE hShm;
  int bReadonly;
};

_Static_assert(offsetof(QpPinnedWinFile, h) == 16,
               "SQLite 3.53.4 winFile HANDLE offset changed");
_Static_assert(offsetof(QpPinnedWinFile, ctrlFlags) == 28,
               "SQLite 3.53.4 winFile flags offset changed");
_Static_assert(offsetof(QpPinnedWinFile, pShm) == 40,
               "SQLite 3.53.4 winFile SHM offset changed");
_Static_assert(offsetof(QpPinnedWinShmNode, hSharedShm) == 16,
               "SQLite 3.53.4 winShmNode HANDLE offset changed");
_Static_assert(offsetof(QpPinnedWinShm, hShm) == 16,
               "SQLite 3.53.4 winShm HANDLE offset changed");
#else
typedef struct QpIdentity {
  dev_t device;
  ino_t inode;
  int valid;
} QpIdentity;

typedef struct QpPinnedUnixShmNode QpPinnedUnixShmNode;
typedef struct QpPinnedUnixShm QpPinnedUnixShm;
typedef struct QpPinnedUnixFile {
  const sqlite3_io_methods *pMethod;
  sqlite3_vfs *pVfs;
  void *pInode;
  int h;
  unsigned char eFileLock;
  unsigned short ctrlFlags;
  int lastErrno;
  void *lockingContext;
  void *pPreallocatedUnused;
  const char *zPath;
  QpPinnedUnixShm *pShm;
} QpPinnedUnixFile;
struct QpPinnedUnixShm {
  QpPinnedUnixShmNode *pShmNode;
};
struct QpPinnedUnixShmNode {
  void *pInode;
  sqlite3_mutex *pShmMutex;
  char *zFilename;
  int hShm;
  int szRegion;
  unsigned short nRegion;
  unsigned char isReadonly;
  unsigned char isUnlocked;
};

_Static_assert(offsetof(QpPinnedUnixFile, h) == 24,
               "SQLite 3.53.4 unixFile descriptor offset changed");
_Static_assert(offsetof(QpPinnedUnixFile, pShm) == 64,
               "SQLite 3.53.4 unixFile SHM offset changed");
_Static_assert(offsetof(QpPinnedUnixShmNode, hShm) == 24,
               "SQLite 3.53.4 unixShmNode descriptor offset changed");
#endif

typedef struct QpFile {
  sqlite3_file sqlite_file;
  sqlite3_file *real;
  int kind;
  int counted_ref;
  QpU64 generation;
  char *path;
  char *placeholder_path;
#ifdef _WIN32
  HANDLE proof_handle;
  HANDLE shm_anchor;
#else
  int proof_handle;
  int shm_anchor;
#endif
  QpIdentity proof_identity;
  QpIdentity actual_identity;
  QpIdentity shm_identity;
  int artifact;
  int source_proved;
  int shm_proved;
  int shm_active;
  int wal_placeholder;
  int sticky_failure_rc;
  int listed;
  struct QpFile *next_active;
} QpFile;

/* Gives the wrapped sqlite3_file the strongest ordinary C alignment without
 * relying on C11 alignof support in older MSVC toolchains. */
typedef union QpFileAlignment {
  sqlite3_file sqlite_file;
  sqlite3_int64 integer;
  long double widest_scalar;
  void *pointer;
} QpFileAlignment;

typedef struct QpFilePrefix {
  QpFile wrapper;
  QpFileAlignment real_alignment;
} QpFilePrefix;

#define QP_REAL_FILE_OFFSET ((int)offsetof(QpFilePrefix, real_alignment))

static const sqlite3_api_routines *qp_api = NULL;
static sqlite3_vfs *qp_base_vfs = NULL;
static sqlite3_vfs qp_vfs;
static sqlite3_mutex *qp_state_mutex = NULL;
static sqlite3_mutex *qp_file_list_mutex = NULL;
static int qp_registered = 0;
#ifdef _WIN32
static QpNtQueryObject qp_nt_query_object = NULL;
#endif
static QpState qp_state;
static QpU64 qp_failure_sequence = 0;
static QpFile *qp_active_file_list = NULL;

static const sqlite3_io_methods qp_io_methods;

static void qp_lock(void) {
  qp_api->mutex_enter(qp_state_mutex);
}

static void qp_unlock(void) {
  qp_api->mutex_leave(qp_state_mutex);
}

static int qp_take_test_cleanup_fault(const char *fault) {
  int result;
  qp_lock();
  result = qp_state.configured && !qp_state.cleanup_fault_fired &&
           strcmp(qp_state.cleanup_fault, fault) == 0;
  if (result) qp_state.cleanup_fault_fired = 1;
  qp_unlock();
  return result;
}

#define QP_AUDIT_INC(field) \
  do { qp_lock(); qp_state.audit.field++; qp_unlock(); } while (0)

static const char *qp_artifact_name(int artifact) {
  switch (artifact) {
    case QP_ARTIFACT_MAIN: return "main";
    case QP_ARTIFACT_WAL: return "wal";
    case QP_ARTIFACT_SHM: return "shm";
    case QP_ARTIFACT_JOURNAL: return "journal";
    case QP_ARTIFACT_TEMP: return "temp";
    default: return "none";
  }
}

static const char *qp_failure_kind_name(int kind) {
  switch (kind) {
    case QP_FAILURE_SOURCE_CHANGED: return "source_changed";
    case QP_FAILURE_UNSUPPORTED: return "unsupported";
    case QP_FAILURE_POLICY: return "policy";
    case QP_FAILURE_IO: return "io";
    default: return "none";
  }
}

static void qp_set_failure_locked(int kind, int artifact,
                                  const char *operation, int sqlite_code) {
  size_t length;
  qp_failure_sequence++;
  if (qp_failure_sequence == 0) qp_failure_sequence = 1;
  qp_state.failure_sequence = qp_failure_sequence;
  qp_state.failure_kind = kind;
  qp_state.failure_artifact = artifact;
  qp_state.failure_sqlite_code = sqlite_code;
  if (operation == NULL) operation = "none";
  length = strlen(operation);
  if (length >= sizeof(qp_state.failure_operation)) {
    length = sizeof(qp_state.failure_operation) - 1;
  }
  memcpy(qp_state.failure_operation, operation, length);
  qp_state.failure_operation[length] = '\0';
}

static void qp_record_failure(int kind, int artifact,
                              const char *operation, int sqlite_code) {
  qp_lock();
  /* A cleanup failure quarantines the process session.  Preserve its exact
   * evidence if an outer operation subsequently reports the ordinary error
   * that led into cleanup; cleanup-specific helpers write status directly. */
  if (!qp_state.cleanup_failed) {
    qp_set_failure_locked(kind, artifact, operation, sqlite_code);
    if (kind == QP_FAILURE_SOURCE_CHANGED) {
      qp_state.audit.identity_rejected++;
    }
  }
  qp_unlock();
}

static void qp_record_base_close_error(int artifact, const char *operation,
                                       int sqlite_code) {
  qp_lock();
  qp_state.audit.base_close_error++;
  qp_state.cleanup_failed = 1;
  qp_set_failure_locked(QP_FAILURE_IO, artifact, operation, sqlite_code);
  qp_unlock();
}

static void qp_record_shm_unmap_error(const char *operation,
                                      int sqlite_code) {
  qp_lock();
  qp_state.audit.shm_unmap_error++;
  qp_state.cleanup_failed = 1;
  qp_set_failure_locked(QP_FAILURE_IO, QP_ARTIFACT_SHM, operation,
                        sqlite_code);
  qp_unlock();
}

static int qp_file_sticky_failure(QpFile *file) {
  int rc = SQLITE_OK;
  if (file == NULL) return SQLITE_OK;
  qp_lock();
  rc = file->sticky_failure_rc;
  if (rc == SQLITE_OK && qp_state.cleanup_failed) rc = SQLITE_IOERR;
  qp_unlock();
  return rc;
}

static int qp_latch_file_failure(QpFile *file, int rc) {
  if (file == NULL || rc == SQLITE_OK) return rc;
  qp_lock();
  if (file->sticky_failure_rc == SQLITE_OK) {
    file->sticky_failure_rc = rc;
  }
  qp_unlock();
  return rc;
}

static int qp_parse_hex_u64(const char *text, QpU64 *value) {
  QpU64 result = 0;
  size_t index;
  size_t length;
  if (text == NULL || value == NULL) return 0;
  length = strlen(text);
  if (length == 0 || length > 16) return 0;
  for (index = 0; index < length; index++) {
    unsigned char c = (unsigned char)text[index];
    unsigned int digit;
    if (c >= '0' && c <= '9') digit = (unsigned int)(c - '0');
    else if (c >= 'a' && c <= 'f') digit = (unsigned int)(c - 'a' + 10);
    else return 0;
    result = (result << 4) | digit;
  }
  *value = result;
  return 1;
}

static size_t qp_strnlen(const char *text, size_t capacity) {
  const char *end;
  if (text == NULL) {
    return 0;
  }
  end = (const char *)memchr(text, '\0', capacity);
  return end == NULL ? capacity : (size_t)(end - text);
}

static int qp_copy_string(char *destination, size_t capacity,
                          const char *source) {
  size_t length = qp_strnlen(source, capacity);
  if (source == NULL || length == 0 || length >= capacity) {
    return 0;
  }
  memcpy(destination, source, length + 1);
  return 1;
}

static char *qp_strdup(const char *source) {
  size_t length;
  char *copy;
  if (source == NULL) {
    return NULL;
  }
  length = strlen(source);
  if (length >= (size_t)INT_MAX) {
    return NULL;
  }
  copy = (char *)qp_api->malloc((int)length + 1);
  if (copy != NULL) {
    memcpy(copy, source, length + 1);
  }
  return copy;
}

static int qp_token_is_valid(const char *token) {
  size_t index;
  size_t length = qp_strnlen(token, QP_TOKEN_CAP);
  if (length == 0 || length >= QP_TOKEN_CAP) {
    return 0;
  }
  for (index = 0; index < length; index++) {
    unsigned char character = (unsigned char)token[index];
    if (!(isalnum(character) || character == '_' || character == '-')) {
      return 0;
    }
  }
  return 1;
}

static int qp_path_is_absolute(const char *path) {
  if (path == NULL || path[0] == '\0') {
    return 0;
  }
#ifdef _WIN32
  /* Network/UNC paths are intentionally unsupported. */
  if ((path[0] == '\\' && path[1] == '\\') ||
      (path[0] == '/' && path[1] == '/')) {
    return 0;
  }
  return isalpha((unsigned char)path[0]) && path[1] == ':' &&
         (path[2] == '\\' || path[2] == '/') &&
         strchr(path + 2, ':') == NULL;
#else
  return path[0] == '/';
#endif
}

#ifdef _WIN32
static wchar_t *qp_utf8_to_wide(const char *path) {
  int count;
  wchar_t *wide;
  count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1,
                              NULL, 0);
  if (count <= 0 || count > QP_PATH_CAP) {
    return NULL;
  }
  wide = (wchar_t *)qp_api->malloc((int)(count * sizeof(wchar_t)));
  if (wide == NULL) {
    return NULL;
  }
  if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1,
                          wide, count) != count) {
    qp_api->free(wide);
    return NULL;
  }
  return wide;
}

static int qp_windows_path_is_local(const wchar_t *wide) {
  wchar_t volume[QP_PATH_CAP];
  wchar_t filesystem_name[64];
  UINT drive_type;
  if (!GetVolumePathNameW(wide, volume, QP_PATH_CAP)) {
    return 0;
  }
  drive_type = GetDriveTypeW(volume);
  if (drive_type != DRIVE_FIXED && drive_type != DRIVE_RAMDISK) return 0;
  if (!GetVolumeInformationW(volume, NULL, 0, NULL, NULL, NULL,
                             filesystem_name,
                             (DWORD)(sizeof(filesystem_name) /
                                     sizeof(filesystem_name[0])))) {
    return 0;
  }
  /* BY_HANDLE_FILE_INFORMATION exposes only a 64-bit file index.  ReFS may
   * require its 128-bit FILE_ID_INFO identity, so this boundary supports
   * NTFS only until that identity is carried end-to-end. */
  return _wcsicmp(filesystem_name, L"NTFS") == 0;
}

static int qp_windows_path_has_no_reparse_component(wchar_t *wide) {
  size_t index;
  size_t length = wcslen(wide);
  for (index = 3; index <= length; index++) {
    if (wide[index] == L'\\' || wide[index] == L'/' || wide[index] == L'\0') {
      wchar_t saved = wide[index];
      DWORD attributes;
      if (index == 3) continue;
      wide[index] = L'\0';
      attributes = GetFileAttributesW(wide);
      wide[index] = saved;
      if (attributes == INVALID_FILE_ATTRIBUTES ||
          (attributes & FILE_ATTRIBUTE_REPARSE_POINT)) {
        return 0;
      }
    }
  }
  return 1;
}

static int qp_validate_existing_path(const char *path, int require_directory) {
  wchar_t *wide;
  DWORD attributes;
  int result = 0;
  if (!qp_path_is_absolute(path)) {
    return 0;
  }
  wide = qp_utf8_to_wide(path);
  if (wide == NULL) {
    return 0;
  }
  attributes = GetFileAttributesW(wide);
  if (attributes != INVALID_FILE_ATTRIBUTES &&
      !(attributes & FILE_ATTRIBUTE_REPARSE_POINT) &&
      (!!(attributes & FILE_ATTRIBUTE_DIRECTORY) == !!require_directory) &&
      qp_windows_path_is_local(wide) &&
      qp_windows_path_has_no_reparse_component(wide)) {
    result = 1;
  }
  qp_api->free(wide);
  return result;
}

static int qp_windows_resolve_nt_query_object(void) {
  HMODULE module;
  FARPROC procedure;
  if (qp_nt_query_object != NULL) return 1;
  module = GetModuleHandleW(L"ntdll.dll");
  if (module == NULL) return 0;
  procedure = GetProcAddress(module, "NtQueryObject");
  if (procedure == NULL || sizeof(procedure) != sizeof(qp_nt_query_object)) {
    return 0;
  }
  memcpy(&qp_nt_query_object, &procedure, sizeof(qp_nt_query_object));
  return qp_nt_query_object != NULL;
}

static int qp_windows_process_is_unprivileged(void) {
  HANDLE token = NULL;
  TOKEN_ELEVATION elevation;
  TOKEN_ELEVATION_TYPE elevation_type;
  DWORD returned = 0;
  int result;
  if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return 0;
  memset(&elevation, 0, sizeof(elevation));
  elevation_type = TokenElevationTypeDefault;
  result = GetTokenInformation(token, TokenElevation, &elevation,
                               (DWORD)sizeof(elevation), &returned) != 0 &&
           returned == sizeof(elevation) &&
           GetTokenInformation(token, TokenElevationType, &elevation_type,
                               (DWORD)sizeof(elevation_type), &returned) != 0 &&
           returned == sizeof(elevation_type) &&
           elevation.TokenIsElevated == 0 &&
           elevation_type != TokenElevationTypeFull;
  if (!CloseHandle(token)) result = 0;
  return result;
}

static int qp_windows_handle_granted_access(HANDLE handle,
                                            ACCESS_MASK *access_out) {
  QpPublicObjectBasicInformation information;
  ULONG returned = 0;
  LONG status;
  if (handle == NULL || handle == INVALID_HANDLE_VALUE ||
      qp_nt_query_object == NULL || access_out == NULL) {
    return 0;
  }
  memset(&information, 0, sizeof(information));
  status = qp_nt_query_object(handle, 0, &information,
                              (ULONG)sizeof(information), &returned);
  if (status < 0 || returned > sizeof(information)) return 0;
  *access_out = information.granted_access;
  return 1;
}

static int qp_windows_handle_is_physically_readonly(HANDLE handle) {
  ACCESS_MASK access;
  const ACCESS_MASK forbidden =
      FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_WRITE_EA |
      FILE_WRITE_ATTRIBUTES | DELETE | WRITE_DAC | WRITE_OWNER |
      GENERIC_WRITE | GENERIC_ALL;
  return qp_windows_handle_granted_access(handle, &access) &&
         (access & forbidden) == 0 && (access & FILE_READ_DATA) != 0;
}

static int qp_windows_handle_is_readwrite(HANDLE handle) {
  ACCESS_MASK access;
  const ACCESS_MASK forbidden = DELETE | WRITE_DAC | WRITE_OWNER | GENERIC_ALL;
  return qp_windows_handle_granted_access(handle, &access) &&
         (access & FILE_READ_DATA) != 0 && (access & FILE_WRITE_DATA) != 0 &&
         (access & forbidden) == 0;
}

static int qp_identity_from_handle(HANDLE handle, QpIdentity *identity) {
  BY_HANDLE_FILE_INFORMATION information;
  QpU64 file_index;
  if (identity == NULL || handle == NULL || handle == INVALID_HANDLE_VALUE ||
      GetFileType(handle) != FILE_TYPE_DISK ||
      !GetFileInformationByHandle(handle, &information) ||
      (information.dwFileAttributes &
       (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT))) {
    return 0;
  }
  file_index = ((QpU64)information.nFileIndexHigh << 32) |
               (QpU64)information.nFileIndexLow;
  if (file_index == 0) return 0;
  identity->volume_serial = information.dwVolumeSerialNumber;
  identity->file_index = file_index;
  identity->valid = 1;
  return 1;
}

static int qp_handle_link_state(HANDLE handle) {
  BY_HANDLE_FILE_INFORMATION information;
  if (handle == NULL || handle == INVALID_HANDLE_VALUE ||
      !GetFileInformationByHandle(handle, &information)) {
    return QP_PATH_IDENTITY_IO;
  }
  return information.nNumberOfLinks == 1 ? QP_PATH_IDENTITY_PRESENT
                                         : QP_PATH_IDENTITY_ALIAS;
}
#else
static int qp_posix_path_has_no_symlink_component(const char *path) {
  char copy[QP_PATH_CAP];
  size_t index;
  size_t length = strlen(path);
  struct stat status;
  if (length == 0 || length >= sizeof(copy)) return 0;
  memcpy(copy, path, length + 1);
  for (index = 1; index <= length; index++) {
    if (copy[index] == '/' || copy[index] == '\0') {
      char saved = copy[index];
      if (index == 1) continue;
      copy[index] = '\0';
      if (lstat(copy, &status) != 0 || S_ISLNK(status.st_mode)) {
        copy[index] = saved;
        return 0;
      }
      copy[index] = saved;
    }
  }
  return 1;
}

static int qp_posix_filesystem_is_supported(const struct statfs *filesystem) {
# ifdef __APPLE__
  return (filesystem->f_flags & MNT_LOCAL) != 0 &&
         (strcmp(filesystem->f_fstypename, "apfs") == 0 ||
          strcmp(filesystem->f_fstypename, "hfs") == 0);
# elif defined(__linux__)
  switch ((unsigned long)filesystem->f_type) {
    case 0x0000EF53UL: /* ext2/ext3/ext4 */
    case 0x58465342UL: /* XFS */
    case 0x9123683EUL: /* btrfs */
    case 0x01021994UL: /* tmpfs */
    case 0x858458F6UL: /* ramfs */
    case 0x794C7630UL: /* overlayfs */
    case 0x2FC12FC1UL: /* OpenZFS */
      return 1;
    default:
      return 0;
  }
# else
  (void)filesystem;
  return 0;
# endif
}

static int qp_posix_descriptor_is_local(int descriptor) {
  struct statfs filesystem;
  return fstatfs(descriptor, &filesystem) == 0 &&
         qp_posix_filesystem_is_supported(&filesystem);
}

static int qp_posix_path_is_local(const char *path) {
  struct statfs filesystem;
  return statfs(path, &filesystem) == 0 &&
         qp_posix_filesystem_is_supported(&filesystem);
}

static int qp_identity_from_descriptor(int descriptor, QpIdentity *identity) {
  struct stat status;
  if (identity == NULL || descriptor < 0 || fstat(descriptor, &status) != 0 ||
      !S_ISREG(status.st_mode) || status.st_ino == 0) {
    return 0;
  }
  identity->device = status.st_dev;
  identity->inode = status.st_ino;
  identity->valid = 1;
  return 1;
}

static int qp_handle_link_state(int descriptor) {
  struct stat status;
  if (descriptor < 0 || fstat(descriptor, &status) != 0) {
    return QP_PATH_IDENTITY_IO;
  }
  return status.st_nlink == 1 ? QP_PATH_IDENTITY_PRESENT
                              : QP_PATH_IDENTITY_ALIAS;
}

static int qp_validate_existing_path(const char *path, int require_directory) {
  struct stat status;
  if (!qp_path_is_absolute(path) || lstat(path, &status) != 0 ||
      S_ISLNK(status.st_mode)) {
    return 0;
  }
  if (require_directory ? !S_ISDIR(status.st_mode) : !S_ISREG(status.st_mode)) {
    return 0;
  }
  if (!qp_posix_path_has_no_symlink_component(path)) {
    return 0;
  }
  return qp_posix_path_is_local(path);
}
#endif

static int qp_identities_equal(const QpIdentity *first,
                               const QpIdentity *second) {
  if (first == NULL || second == NULL || !first->valid || !second->valid) {
    return 0;
  }
#ifdef _WIN32
  return first->volume_serial == second->volume_serial &&
         first->file_index == second->file_index;
#else
  return first->device == second->device && first->inode == second->inode;
#endif
}

static void qp_note_proof_open(void) {
  qp_lock();
  qp_state.audit.proof_open++;
  qp_state.audit.proof_active++;
  if (qp_state.audit.proof_active > qp_state.audit.proof_peak) {
    qp_state.audit.proof_peak = qp_state.audit.proof_active;
  }
  qp_unlock();
}

static void qp_note_proof_close(int succeeded, int artifact,
                                const char *operation) {
  qp_lock();
  qp_state.audit.proof_close++;
  if (succeeded) {
    if (qp_state.audit.proof_active > 0) {
      qp_state.audit.proof_active--;
    } else {
      qp_state.audit.proof_close_error++;
      qp_state.cleanup_failed = 1;
      qp_set_failure_locked(QP_FAILURE_IO, artifact,
                            "proof_counter_underflow", SQLITE_IOERR);
    }
  } else {
    qp_state.audit.proof_close_error++;
    qp_state.cleanup_failed = 1;
    qp_set_failure_locked(QP_FAILURE_IO, artifact, operation, SQLITE_IOERR);
  }
  qp_unlock();
}

#ifdef _WIN32
static int qp_proof_handle_is_valid(HANDLE handle) {
  return handle != NULL && handle != INVALID_HANDLE_VALUE;
}

static int qp_close_proof_handle(HANDLE *handle, int artifact,
                                 const char *operation);

static int qp_open_proof_handle(const char *path, HANDLE *handle_out,
                                QpIdentity *identity_out, int artifact) {
  wchar_t *wide = NULL;
  HANDLE handle = INVALID_HANDLE_VALUE;
  QpIdentity handle_identity;
  QpIdentity path_identity;
  HANDLE path_handle = INVALID_HANDLE_VALUE;
  int result = 0;
  int cleanup_error = 0;
  memset(&handle_identity, 0, sizeof(handle_identity));
  memset(&path_identity, 0, sizeof(path_identity));
  if (!qp_path_is_absolute(path)) return SQLITE_CANTOPEN;
  wide = qp_utf8_to_wide(path);
  if (wide == NULL) return SQLITE_NOMEM;
  if (!qp_windows_path_is_local(wide) ||
      !qp_windows_path_has_no_reparse_component(wide)) {
    qp_api->free(wide);
    return SQLITE_CANTOPEN;
  }
  handle = CreateFileW(wide, GENERIC_READ,
                       FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                       NULL, OPEN_EXISTING,
                       FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
                       NULL);
  if (!qp_proof_handle_is_valid(handle)) {
    qp_api->free(wide);
    return SQLITE_CANTOPEN;
  }
  qp_note_proof_open();
  if (!qp_identity_from_handle(handle, &handle_identity) ||
      !qp_windows_handle_is_physically_readonly(handle)) {
    int close_ok = qp_close_proof_handle(&handle, artifact,
                                         "proof_reject_close");
    qp_api->free(wide);
    return close_ok ? SQLITE_CANTOPEN : SQLITE_IOERR;
  }
  /* Re-open the name for attributes and compare the identity observed at
   * both points.  Under the trusted-parent-namespace contract this detects
   * a replacement visible during acquisition; FILE_SHARE_DELETE remains
   * enabled so a legitimate live writer may replace WAL state later. */
  path_handle = CreateFileW(
      wide, FILE_READ_ATTRIBUTES,
      FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL,
      OPEN_EXISTING,
      FILE_ATTRIBUTE_NORMAL | FILE_FLAG_BACKUP_SEMANTICS |
          FILE_FLAG_OPEN_REPARSE_POINT,
      NULL);
  if (qp_proof_handle_is_valid(path_handle)) {
    qp_note_proof_open();
    if (qp_identity_from_handle(path_handle, &path_identity) &&
        qp_identities_equal(&handle_identity, &path_identity)) {
      result = 1;
    }
    if (!qp_close_proof_handle(&path_handle, artifact,
                               "proof_path_close")) {
      result = 0;
      cleanup_error = 1;
    }
  }
  qp_api->free(wide);
  if (!result) {
    if (!qp_close_proof_handle(&handle, artifact, "proof_reject_close")) {
      cleanup_error = 1;
    }
    return cleanup_error ? SQLITE_IOERR : SQLITE_CANTOPEN;
  }
  *handle_out = handle;
  *identity_out = handle_identity;
  return SQLITE_OK;
}

static int qp_current_path_identity(const char *path, QpIdentity *identity,
                                    int artifact) {
  wchar_t *wide;
  HANDLE handle = INVALID_HANDLE_VALUE;
  DWORD attributes;
  DWORD error;
  int result = QP_PATH_IDENTITY_IO;
  memset(identity, 0, sizeof(*identity));
  if (!qp_path_is_absolute(path)) return QP_PATH_IDENTITY_UNSAFE;
  wide = qp_utf8_to_wide(path);
  if (wide == NULL) return QP_PATH_IDENTITY_IO;
  attributes = GetFileAttributesW(wide);
  if (attributes == INVALID_FILE_ATTRIBUTES) {
    error = GetLastError();
    qp_api->free(wide);
    return error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND
               ? QP_PATH_IDENTITY_MISSING
               : QP_PATH_IDENTITY_IO;
  }
  if ((attributes & (FILE_ATTRIBUTE_DIRECTORY |
                     FILE_ATTRIBUTE_REPARSE_POINT)) != 0 ||
      !qp_windows_path_is_local(wide) ||
      !qp_windows_path_has_no_reparse_component(wide)) {
    qp_api->free(wide);
    return QP_PATH_IDENTITY_UNSAFE;
  }
  handle = CreateFileW(
      wide, FILE_READ_ATTRIBUTES,
      FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL,
      OPEN_EXISTING,
      FILE_ATTRIBUTE_NORMAL | FILE_FLAG_BACKUP_SEMANTICS |
          FILE_FLAG_OPEN_REPARSE_POINT,
      NULL);
  error = qp_proof_handle_is_valid(handle) ? ERROR_SUCCESS : GetLastError();
  qp_api->free(wide);
  if (!qp_proof_handle_is_valid(handle)) {
    return error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND
               ? QP_PATH_IDENTITY_MISSING
               : QP_PATH_IDENTITY_IO;
  }
  qp_note_proof_open();
  result = qp_identity_from_handle(handle, identity)
               ? QP_PATH_IDENTITY_PRESENT
               : QP_PATH_IDENTITY_IO;
  if (!qp_close_proof_handle(&handle, artifact, "path_identity_close")) {
    return QP_PATH_IDENTITY_IO;
  }
  return result;
}

static int qp_close_proof_handle(HANDLE *handle, int artifact,
                                 const char *operation) {
  int succeeded;
  if (!qp_proof_handle_is_valid(*handle)) return 1;
  succeeded = CloseHandle(*handle) != 0;
  if (succeeded && qp_take_test_cleanup_fault("proof_close")) succeeded = 0;
  *handle = INVALID_HANDLE_VALUE;
  qp_note_proof_close(succeeded, artifact, operation);
  return succeeded;
}
#else
static int qp_proof_handle_is_valid(int descriptor) {
  return descriptor >= 0;
}

static int qp_close_proof_handle(int *descriptor, int artifact,
                                 const char *operation);

static int qp_open_proof_handle(const char *path, int *handle_out,
                                QpIdentity *identity_out, int artifact) {
  struct stat path_status;
  QpIdentity handle_identity;
  QpIdentity path_identity;
  int descriptor;
  memset(&handle_identity, 0, sizeof(handle_identity));
  memset(&path_identity, 0, sizeof(path_identity));
  if (!qp_path_is_absolute(path) ||
      !qp_posix_path_has_no_symlink_component(path)) {
    return SQLITE_CANTOPEN;
  }
  descriptor = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  if (descriptor < 0) {
    return errno == ELOOP ? SQLITE_CANTOPEN_SYMLINK : SQLITE_CANTOPEN;
  }
  qp_note_proof_open();
  if (!qp_identity_from_descriptor(descriptor, &handle_identity) ||
      !qp_posix_descriptor_is_local(descriptor) ||
      lstat(path, &path_status) != 0 || S_ISLNK(path_status.st_mode) ||
      !S_ISREG(path_status.st_mode)) {
    return qp_close_proof_handle(&descriptor, artifact,
                                 "proof_reject_close")
               ? SQLITE_CANTOPEN
               : SQLITE_IOERR;
  }
  path_identity.device = path_status.st_dev;
  path_identity.inode = path_status.st_ino;
  path_identity.valid = path_status.st_ino != 0;
  if (!qp_identities_equal(&handle_identity, &path_identity)) {
    return qp_close_proof_handle(&descriptor, artifact,
                                 "proof_reject_close")
               ? SQLITE_CANTOPEN
               : SQLITE_IOERR;
  }
  *handle_out = descriptor;
  *identity_out = handle_identity;
  return SQLITE_OK;
}

static int qp_current_path_identity(const char *path, QpIdentity *identity,
                                    int artifact) {
  struct stat status;
  struct statfs filesystem;
  char copy[QP_PATH_CAP];
  size_t index;
  size_t length;
  memset(identity, 0, sizeof(*identity));
  (void)artifact;
  if (!qp_path_is_absolute(path)) return QP_PATH_IDENTITY_UNSAFE;
  if (lstat(path, &status) != 0) {
    return errno == ENOENT || errno == ENOTDIR ? QP_PATH_IDENTITY_MISSING
                                               : QP_PATH_IDENTITY_IO;
  }
  if (S_ISLNK(status.st_mode) || !S_ISREG(status.st_mode) ||
      status.st_ino == 0) {
    return QP_PATH_IDENTITY_UNSAFE;
  }
  length = strlen(path);
  if (length == 0 || length >= sizeof(copy)) return QP_PATH_IDENTITY_UNSAFE;
  memcpy(copy, path, length + 1);
  for (index = 1; index <= length; index++) {
    if (copy[index] == '/' || copy[index] == '\0') {
      char saved = copy[index];
      if (index == 1) continue;
      copy[index] = '\0';
      if (lstat(copy, &status) != 0) {
        int saved_errno = errno;
        copy[index] = saved;
        return saved_errno == ENOENT || saved_errno == ENOTDIR
                   ? QP_PATH_IDENTITY_MISSING
                   : QP_PATH_IDENTITY_IO;
      }
      copy[index] = saved;
      if (S_ISLNK(status.st_mode)) return QP_PATH_IDENTITY_UNSAFE;
    }
  }
  if (statfs(path, &filesystem) != 0) return QP_PATH_IDENTITY_IO;
  if (!qp_posix_filesystem_is_supported(&filesystem)) {
    return QP_PATH_IDENTITY_UNSAFE;
  }
  if (lstat(path, &status) != 0) {
    return errno == ENOENT || errno == ENOTDIR ? QP_PATH_IDENTITY_MISSING
                                               : QP_PATH_IDENTITY_IO;
  }
  if (S_ISLNK(status.st_mode) || !S_ISREG(status.st_mode) ||
      status.st_ino == 0) {
    return QP_PATH_IDENTITY_UNSAFE;
  }
  identity->device = status.st_dev;
  identity->inode = status.st_ino;
  identity->valid = 1;
  return QP_PATH_IDENTITY_PRESENT;
}

static int qp_close_proof_handle(int *descriptor, int artifact,
                                 const char *operation) {
  int succeeded;
  if (!qp_proof_handle_is_valid(*descriptor)) return 1;
  succeeded = close(*descriptor) == 0;
  if (succeeded && qp_take_test_cleanup_fault("proof_close")) succeeded = 0;
  /* Never retry close(2) after EINTR: on several supported kernels the file
   * descriptor has already been consumed and may have been reused. */
  *descriptor = -1;
  qp_note_proof_close(succeeded, artifact, operation);
  return succeeded;
}
#endif

#ifdef _WIN32
static int qp_open_private_temp_proof(const char *path, HANDLE *handle_out,
                                      QpIdentity *identity_out) {
  wchar_t *wide = qp_utf8_to_wide(path);
  HANDLE handle;
  if (wide == NULL) return SQLITE_NOMEM;
  if (!qp_windows_path_is_local(wide)) {
    qp_api->free(wide);
    return SQLITE_CANTOPEN;
  }
  handle = CreateFileW(wide, GENERIC_READ,
                       FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                       NULL, OPEN_EXISTING,
                       FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
                       NULL);
  qp_api->free(wide);
  if (!qp_proof_handle_is_valid(handle)) return SQLITE_CANTOPEN;
  qp_note_proof_open();
  if (!qp_identity_from_handle(handle, identity_out) ||
      !qp_windows_handle_is_physically_readonly(handle)) {
    return qp_close_proof_handle(&handle, QP_ARTIFACT_TEMP,
                                 "temp_proof_reject_close")
               ? SQLITE_CANTOPEN
               : SQLITE_IOERR;
  }
  *handle_out = handle;
  return SQLITE_OK;
}
#else
static int qp_open_private_temp_proof(const char *path, int *handle_out,
                                      QpIdentity *identity_out) {
  struct stat path_status;
  QpIdentity path_identity;
  int descriptor = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  memset(&path_identity, 0, sizeof(path_identity));
  if (descriptor < 0) return SQLITE_CANTOPEN;
  qp_note_proof_open();
  if (!qp_identity_from_descriptor(descriptor, identity_out) ||
      !qp_posix_descriptor_is_local(descriptor) ||
      lstat(path, &path_status) != 0 || S_ISLNK(path_status.st_mode) ||
      !S_ISREG(path_status.st_mode)) {
    return qp_close_proof_handle(&descriptor, QP_ARTIFACT_TEMP,
                                 "temp_proof_reject_close")
               ? SQLITE_CANTOPEN
               : SQLITE_IOERR;
  }
  path_identity.device = path_status.st_dev;
  path_identity.inode = path_status.st_ino;
  path_identity.valid = path_status.st_ino != 0;
  if (!qp_identities_equal(identity_out, &path_identity)) {
    return qp_close_proof_handle(&descriptor, QP_ARTIFACT_TEMP,
                                 "temp_proof_reject_close")
               ? SQLITE_CANTOPEN
               : SQLITE_IOERR;
  }
  *handle_out = descriptor;
  return SQLITE_OK;
}
#endif

static uint32_t qp_load_u32_be(const unsigned char *value) {
  return ((uint32_t)value[0] << 24) | ((uint32_t)value[1] << 16) |
         ((uint32_t)value[2] << 8) | (uint32_t)value[3];
}

static uint32_t qp_load_u32_le(const unsigned char *value) {
  return (uint32_t)value[0] | ((uint32_t)value[1] << 8) |
         ((uint32_t)value[2] << 16) | ((uint32_t)value[3] << 24);
}

static int qp_wal_header_bytes_are_valid(
    const unsigned char header[QP_WAL_HEADER_SIZE]) {
  uint32_t magic = qp_load_u32_be(header);
  uint32_t version = qp_load_u32_be(header + 4);
  uint32_t page_size = qp_load_u32_be(header + 8);
  uint32_t first = 0;
  uint32_t second = 0;
  int checksum_is_big_endian = (magic & 1U) != 0;
  size_t offset;
  if ((magic & ~1U) != QP_WAL_MAGIC ||
      version != QP_WAL_FORMAT_VERSION || page_size < 512U ||
      page_size > 65536U || (page_size & (page_size - 1U)) != 0) {
    return 0;
  }
  for (offset = 0; offset < 24; offset += 8) {
    uint32_t word0 = checksum_is_big_endian
                         ? qp_load_u32_be(header + offset)
                         : qp_load_u32_le(header + offset);
    uint32_t word1 = checksum_is_big_endian
                         ? qp_load_u32_be(header + offset + 4)
                         : qp_load_u32_le(header + offset + 4);
    first += word0 + second;
    second += word1 + first;
  }
  return first == qp_load_u32_be(header + 24) &&
         second == qp_load_u32_be(header + 28);
}

#ifdef _WIN32
static int qp_validate_wal_proof_header(HANDLE handle, int *failure_kind) {
  unsigned char header[QP_WAL_HEADER_SIZE];
  LARGE_INTEGER size;
  LARGE_INTEGER beginning;
  DWORD amount = 0;
  beginning.QuadPart = 0;
  if (!GetFileSizeEx(handle, &size) || size.QuadPart < 0) {
    *failure_kind = QP_FAILURE_IO;
    return SQLITE_IOERR_FSTAT;
  }
  if (size.QuadPart == 0) return SQLITE_OK;
  if (size.QuadPart < QP_WAL_HEADER_SIZE) {
    *failure_kind = QP_FAILURE_UNSUPPORTED;
    return SQLITE_CANTOPEN;
  }
  if (!SetFilePointerEx(handle, beginning, NULL, FILE_BEGIN) ||
      !ReadFile(handle, header, sizeof(header), &amount, NULL) ||
      amount != sizeof(header)) {
    *failure_kind = QP_FAILURE_IO;
    return SQLITE_IOERR;
  }
  qp_lock();
  qp_state.audit.source_read++;
  qp_state.audit.source_read_bytes += sizeof(header);
  qp_unlock();
  if (!qp_wal_header_bytes_are_valid(header)) {
    *failure_kind = QP_FAILURE_UNSUPPORTED;
    return SQLITE_CANTOPEN;
  }
  return SQLITE_OK;
}
#else
static int qp_validate_wal_proof_header(int descriptor, int *failure_kind) {
  unsigned char header[QP_WAL_HEADER_SIZE];
  struct stat status;
  ssize_t amount;
  if (fstat(descriptor, &status) != 0 || status.st_size < 0) {
    *failure_kind = QP_FAILURE_IO;
    return SQLITE_IOERR_FSTAT;
  }
  if (status.st_size == 0) return SQLITE_OK;
  if (status.st_size < QP_WAL_HEADER_SIZE) {
    *failure_kind = QP_FAILURE_UNSUPPORTED;
    return SQLITE_CANTOPEN;
  }
  do {
    amount = pread(descriptor, header, sizeof(header), 0);
  } while (amount < 0 && errno == EINTR);
  if (amount != (ssize_t)sizeof(header)) {
    *failure_kind = QP_FAILURE_IO;
    return SQLITE_IOERR;
  }
  qp_lock();
  qp_state.audit.source_read++;
  qp_state.audit.source_read_bytes += sizeof(header);
  qp_unlock();
  if (!qp_wal_header_bytes_are_valid(header)) {
    *failure_kind = QP_FAILURE_UNSUPPORTED;
    return SQLITE_CANTOPEN;
  }
  return SQLITE_OK;
}
#endif

static int qp_parse_expected_identity(sqlite3_filename name,
                                      QpIdentity *expected) {
  const char *kind = qp_api->uri_parameter(name, "qplot_expected_kind");
  const char *first = qp_api->uri_parameter(name, "qplot_expected_a");
  const char *second = qp_api->uri_parameter(name, "qplot_expected_b");
  QpU64 a;
  QpU64 b;
  memset(expected, 0, sizeof(*expected));
  if (!qp_parse_hex_u64(first, &a) || !qp_parse_hex_u64(second, &b)) {
    return 0;
  }
#ifdef _WIN32
  if (kind == NULL || strcmp(kind, "windows") != 0 || a > 0xffffffffULL ||
      b == 0) {
    return 0;
  }
  expected->volume_serial = (DWORD)a;
  expected->file_index = b;
#else
  if (kind == NULL || strcmp(kind, "posix") != 0 || b == 0) return 0;
  expected->device = (dev_t)a;
  expected->inode = (ino_t)b;
  if ((QpU64)expected->device != a || (QpU64)expected->inode != b) return 0;
#endif
  expected->valid = 1;
  return 1;
}

static int qp_parse_optional_expected_identity(
    sqlite3_filename name, const char *kind_parameter,
    const char *first_parameter, const char *second_parameter, int *present,
    QpU64 *a_out, QpU64 *b_out) {
  const char *kind = qp_api->uri_parameter(name, kind_parameter);
  const char *first = qp_api->uri_parameter(name, first_parameter);
  const char *second = qp_api->uri_parameter(name, second_parameter);
  QpU64 a;
  QpU64 b;
  if (kind == NULL) return 0;
  if (strcmp(kind, "absent") == 0) {
    if (first != NULL || second != NULL) return 0;
    *present = 0;
    *a_out = 0;
    *b_out = 0;
    return 1;
  }
#ifdef _WIN32
  if (strcmp(kind, "windows") != 0 || !qp_parse_hex_u64(first, &a) ||
      !qp_parse_hex_u64(second, &b) || a > 0xffffffffULL || b == 0) {
    return 0;
  }
#else
  if (strcmp(kind, "posix") != 0 || !qp_parse_hex_u64(first, &a) ||
      !qp_parse_hex_u64(second, &b) || b == 0 ||
      (QpU64)(dev_t)a != a || (QpU64)(ino_t)b != b) {
    return 0;
  }
#endif
  *present = 1;
  *a_out = a;
  *b_out = b;
  return 1;
}

static int qp_expected_sidecar_requires_present(int artifact) {
  int result = 0;
  qp_lock();
  if (artifact == QP_ARTIFACT_WAL) {
    result = qp_state.expected_wal_present;
  } else if (artifact == QP_ARTIFACT_SHM) {
    result = qp_state.expected_shm_present;
  }
  qp_unlock();
  return result;
}

static int qp_accept_expected_sidecar_identity(int artifact,
                                               const QpIdentity *identity) {
  int accepted = 0;
  QpU64 identity_a;
  QpU64 identity_b;
  if (identity == NULL || !identity->valid) return 0;
#ifdef _WIN32
  identity_a = (QpU64)identity->volume_serial;
  identity_b = identity->file_index;
#else
  identity_a = (QpU64)identity->device;
  identity_b = (QpU64)identity->inode;
#endif
  qp_lock();
  if (artifact == QP_ARTIFACT_WAL) {
    if (qp_state.expected_wal_present) {
      accepted = qp_state.expected_wal_a == identity_a &&
                 qp_state.expected_wal_b == identity_b;
    } else {
      qp_state.expected_wal_present = 1;
      qp_state.expected_wal_a = identity_a;
      qp_state.expected_wal_b = identity_b;
      accepted = 1;
    }
    if (accepted) qp_state.expected_wal_pending = 0;
  } else if (artifact == QP_ARTIFACT_SHM) {
    if (qp_state.expected_shm_present) {
      accepted = qp_state.expected_shm_a == identity_a &&
                 qp_state.expected_shm_b == identity_b;
    } else {
      qp_state.expected_shm_present = 1;
      qp_state.expected_shm_a = identity_a;
      qp_state.expected_shm_b = identity_b;
      accepted = 1;
    }
    if (accepted) qp_state.expected_shm_pending = 0;
  }
  qp_unlock();
  if (!accepted) {
    qp_record_failure(QP_FAILURE_SOURCE_CHANGED, artifact,
                      "expected_sidecar_identity", SQLITE_CANTOPEN);
  }
  return accepted;
}

static int qp_open_source_proof(QpFile *file) {
  int rc;
  if (file->source_proved) return SQLITE_OK;
  rc = qp_open_proof_handle(file->path, &file->proof_handle,
                            &file->proof_identity, file->artifact);
  if (rc == SQLITE_OK) file->source_proved = 1;
  return rc;
}

static int qp_close_source_proof(QpFile *file) {
  int succeeded;
  int proof_artifact = file->wal_placeholder ? QP_ARTIFACT_TEMP
                                             : file->artifact;
  const char *operation = file->wal_placeholder
                              ? "wal_placeholder_proof_close"
                              : "source_proof_close";
  if (!file->source_proved && !qp_proof_handle_is_valid(file->proof_handle)) {
    return SQLITE_OK;
  }
  succeeded = qp_close_proof_handle(&file->proof_handle, proof_artifact,
                                    operation);
  memset(&file->proof_identity, 0, sizeof(file->proof_identity));
  memset(&file->actual_identity, 0, sizeof(file->actual_identity));
  file->source_proved = 0;
  if (!succeeded) (void)qp_latch_file_failure(file, SQLITE_IOERR);
  return succeeded ? SQLITE_OK : SQLITE_IOERR;
}

static int qp_append_suffix(const char *path, const char *suffix,
                            char **result);
static int qp_clear_shm_proof(QpFile *file);

static int qp_open_shm_proof(QpFile *file, int *failure_recorded) {
  char *path = NULL;
  int link_state;
  int rc;
  if (failure_recorded != NULL) *failure_recorded = 0;
  if (file->shm_proved) return SQLITE_OK;
  rc = qp_append_suffix(file->path, "-shm", &path);
  if (rc != SQLITE_OK) return rc;
  rc = qp_open_proof_handle(path, &file->shm_anchor, &file->shm_identity,
                            QP_ARTIFACT_SHM);
  qp_api->free(path);
  if (rc == SQLITE_OK) {
    link_state = qp_handle_link_state(file->shm_anchor);
    if (link_state != QP_PATH_IDENTITY_PRESENT) {
      int link_rc = link_state == QP_PATH_IDENTITY_ALIAS
                        ? SQLITE_CANTOPEN
                        : SQLITE_IOERR_FSTAT;
      qp_record_failure(link_state == QP_PATH_IDENTITY_ALIAS
                            ? QP_FAILURE_UNSUPPORTED
                            : QP_FAILURE_IO,
                        QP_ARTIFACT_SHM,
                        link_state == QP_PATH_IDENTITY_ALIAS
                            ? "shm_hardlink"
                            : "shm_proof_link",
                        link_rc);
      if (failure_recorded != NULL) *failure_recorded = 1;
      /* Once delegated SHM is active, retain even a rejected proof until
       * xShmUnmap has released SQLite's POSIX locks.  Closing any descriptor
       * for the inode first could drop all process-owned fcntl locks. */
      if (!file->shm_active &&
          !qp_close_proof_handle(&file->shm_anchor, QP_ARTIFACT_SHM,
                                 "shm_proof_close") &&
          failure_recorded != NULL) {
        *failure_recorded = 1;
      }
      memset(&file->shm_identity, 0, sizeof(file->shm_identity));
      return link_rc;
    }
    file->shm_proved = 1;
    if (!qp_accept_expected_sidecar_identity(QP_ARTIFACT_SHM,
                                             &file->shm_identity)) {
      if (failure_recorded != NULL) *failure_recorded = 1;
      if (!file->shm_active && qp_clear_shm_proof(file) != SQLITE_OK &&
          failure_recorded != NULL) {
        *failure_recorded = 1;
      }
      return SQLITE_CANTOPEN;
    }
  }
  return rc;
}

static int qp_clear_shm_proof(QpFile *file) {
  int succeeded = 1;
  if (file->shm_proved || qp_proof_handle_is_valid(file->shm_anchor)) {
    succeeded = qp_close_proof_handle(&file->shm_anchor, QP_ARTIFACT_SHM,
                                      "shm_proof_close");
  }
  memset(&file->shm_identity, 0, sizeof(file->shm_identity));
  file->shm_proved = 0;
  file->shm_active = 0;
  if (!succeeded) (void)qp_latch_file_failure(file, SQLITE_IOERR);
  return succeeded ? SQLITE_OK : SQLITE_IOERR;
}

static void qp_active_file_add(QpFile *file) {
  qp_api->mutex_enter(qp_file_list_mutex);
  file->next_active = qp_active_file_list;
  qp_active_file_list = file;
  file->listed = 1;
  qp_api->mutex_leave(qp_file_list_mutex);
}

static void qp_active_file_remove(QpFile *file) {
  QpFile **link;
  int found = 0;
  int missing = 0;
  qp_api->mutex_enter(qp_file_list_mutex);
  if (file->listed) {
    for (link = &qp_active_file_list; *link != NULL;
         link = &(*link)->next_active) {
      if (*link == file) {
        *link = file->next_active;
        found = 1;
        break;
      }
    }
    missing = !found;
  }
  file->listed = 0;
  file->next_active = NULL;
  qp_api->mutex_leave(qp_file_list_mutex);
  if (missing) {
    qp_lock();
    qp_state.cleanup_failed = 1;
    qp_set_failure_locked(QP_FAILURE_IO, file->artifact,
                          "file_list_missing", SQLITE_IOERR);
    qp_unlock();
  }
}

static int qp_get_actual_source_identity(QpFile *file, QpIdentity *identity,
                                         int *failure_kind) {
  const char *binding_path = file->wal_placeholder ? file->placeholder_path
                                                   : file->path;
  memset(identity, 0, sizeof(*identity));
#ifdef _WIN32
  (void)binding_path;
  {
    QpPinnedWinFile *actual = (QpPinnedWinFile *)file->real;
    HANDLE public_handle = INVALID_HANDLE_VALUE;
    int rc;
    if (actual->pMethod != file->real->pMethods ||
        actual->pVfs != qp_base_vfs) {
      *failure_kind = QP_FAILURE_UNSUPPORTED;
      return SQLITE_CANTOPEN;
    }
    rc = file->real->pMethods->xFileControl(
        file->real, SQLITE_FCNTL_WIN32_GET_HANDLE, &public_handle);
    if (rc != SQLITE_OK || public_handle != actual->h ||
        !qp_proof_handle_is_valid(public_handle)) {
      *failure_kind = QP_FAILURE_UNSUPPORTED;
      return rc == SQLITE_OK ? SQLITE_CANTOPEN : rc;
    }
    if ((actual->ctrlFlags & 0x02) == 0) {
      *failure_kind = QP_FAILURE_POLICY;
      return SQLITE_READONLY;
    }
    if (!qp_windows_handle_is_physically_readonly(public_handle)) {
      *failure_kind = QP_FAILURE_POLICY;
      return SQLITE_READONLY;
    }
    if (!qp_identity_from_handle(public_handle, identity)) {
      *failure_kind = QP_FAILURE_IO;
      return SQLITE_IOERR_FSTAT;
    }
  }
#else
  {
    QpPinnedUnixFile *actual = (QpPinnedUnixFile *)file->real;
    int open_flags;
    if (actual->pMethod != file->real->pMethods ||
        actual->pVfs != qp_base_vfs ||
        (file->artifact == QP_ARTIFACT_MAIN && actual->pInode == NULL) ||
        actual->zPath == NULL || binding_path == NULL ||
        strcmp(actual->zPath, binding_path) != 0 ||
        actual->h < 0) {
      *failure_kind = QP_FAILURE_UNSUPPORTED;
      return SQLITE_CANTOPEN;
    }
    open_flags = fcntl(actual->h, F_GETFL);
    if (open_flags < 0 ||
        !qp_identity_from_descriptor(actual->h, identity) ||
        !qp_posix_descriptor_is_local(actual->h)) {
      *failure_kind = QP_FAILURE_IO;
      return SQLITE_IOERR_FSTAT;
    }
    if ((actual->ctrlFlags & 0x02) == 0 ||
        (open_flags & O_ACCMODE) != O_RDONLY) {
      *failure_kind = QP_FAILURE_POLICY;
      return SQLITE_READONLY;
    }
  }
#endif
  *failure_kind = QP_FAILURE_NONE;
  return SQLITE_OK;
}

static int qp_validate_source_binding_now(QpFile *file,
                                          const char *operation) {
  QpIdentity proof_now;
  QpIdentity actual_now;
  QpIdentity path_now;
  int failure_kind = QP_FAILURE_IO;
  int path_state;
  int rc;
  const char *binding_path;
  memset(&proof_now, 0, sizeof(proof_now));
  memset(&actual_now, 0, sizeof(actual_now));
  memset(&path_now, 0, sizeof(path_now));
  if (file == NULL || file->kind != QP_FILE_SOURCE ||
      !file->source_proved ||
      !qp_proof_handle_is_valid(file->proof_handle)) {
    qp_record_failure(QP_FAILURE_UNSUPPORTED,
                      file == NULL ? QP_ARTIFACT_NONE : file->artifact,
                      operation, SQLITE_CANTOPEN);
    return SQLITE_CANTOPEN;
  }
  binding_path = file->wal_placeholder ? file->placeholder_path : file->path;
  if (binding_path == NULL) {
    qp_record_failure(QP_FAILURE_UNSUPPORTED, file->artifact, operation,
                      SQLITE_CANTOPEN);
    return SQLITE_CANTOPEN;
  }
#ifdef _WIN32
  if (!qp_windows_handle_is_physically_readonly(file->proof_handle)) {
    qp_record_failure(QP_FAILURE_POLICY, file->artifact, operation,
                      SQLITE_READONLY);
    return SQLITE_READONLY;
  }
  if (!qp_identity_from_handle(file->proof_handle, &proof_now)) {
#else
  if (!qp_identity_from_descriptor(file->proof_handle, &proof_now)) {
#endif
    qp_record_failure(QP_FAILURE_IO, file->artifact, operation,
                      SQLITE_IOERR_FSTAT);
    return SQLITE_IOERR_FSTAT;
  }
  if (!qp_identities_equal(&proof_now, &file->proof_identity)) {
    qp_record_failure(QP_FAILURE_SOURCE_CHANGED, file->artifact, operation,
                      SQLITE_IOERR);
    return SQLITE_IOERR;
  }
  rc = qp_get_actual_source_identity(file, &actual_now, &failure_kind);
  if (rc != SQLITE_OK) {
    qp_record_failure(failure_kind, file->artifact, operation, rc);
    return rc;
  }
  if (!qp_identities_equal(&actual_now, &file->proof_identity) ||
      (file->actual_identity.valid &&
       !qp_identities_equal(&actual_now, &file->actual_identity))) {
    qp_record_failure(QP_FAILURE_SOURCE_CHANGED, file->artifact, operation,
                      SQLITE_IOERR);
    return SQLITE_IOERR;
  }
  path_state = qp_current_path_identity(binding_path, &path_now,
                                        file->artifact);
  if (path_state != QP_PATH_IDENTITY_PRESENT ||
      !qp_identities_equal(&path_now, &file->proof_identity)) {
    int sticky_rc = qp_file_sticky_failure(file);
    if (sticky_rc != SQLITE_OK) return sticky_rc;
    if (path_state == QP_PATH_IDENTITY_UNSAFE) {
      qp_record_failure(QP_FAILURE_UNSUPPORTED, file->artifact, operation,
                        SQLITE_CANTOPEN);
      return SQLITE_CANTOPEN;
    }
    if (path_state == QP_PATH_IDENTITY_IO) {
      qp_record_failure(QP_FAILURE_IO, file->artifact, operation,
                        SQLITE_IOERR);
      return SQLITE_IOERR;
    }
    qp_record_failure(QP_FAILURE_SOURCE_CHANGED, file->artifact, operation,
                      SQLITE_IOERR);
    return SQLITE_IOERR;
  }
  file->actual_identity = actual_now;
  QP_AUDIT_INC(identity_verified);
  return SQLITE_OK;
}

static int qp_validate_source_binding(QpFile *file, const char *operation) {
  int rc = qp_file_sticky_failure(file);
  if (rc != SQLITE_OK) return rc;
  return qp_latch_file_failure(
      file, qp_validate_source_binding_now(file, operation));
}

static int qp_append_suffix(const char *path, const char *suffix,
                            char **result) {
  size_t path_length = strlen(path);
  size_t suffix_length = strlen(suffix);
  char *value;
  if (path_length + suffix_length >= QP_PATH_CAP ||
      path_length + suffix_length >= (size_t)INT_MAX) {
    return SQLITE_CANTOPEN;
  }
  value = (char *)qp_api->malloc((int)(path_length + suffix_length + 1));
  if (value == NULL) {
    return SQLITE_NOMEM;
  }
  memcpy(value, path, path_length);
  memcpy(value + path_length, suffix, suffix_length + 1);
  *result = value;
  return SQLITE_OK;
}

/* Trusted-live rollback-journal sources are sidecar-free.  SQLite does not
 * necessarily request SQLITE_OPEN_MAIN_JOURNAL merely because a stray
 * journal pathname appears (in particular while reading WAL mode), so the
 * token-gated operation boundary also checks that the name remains absent.
 * POSIX current-path inspection is lstat/statfs-only and therefore cannot
 * disturb SQLite's process-owned fcntl locks.  Windows uses a checked,
 * read-attributes-only handle when the name denotes a regular file. */
static int qp_validate_no_rollback_journal(QpFile *file) {
  char *path = NULL;
  QpIdentity unused_identity;
  int path_state;
  int rc;
  if (file == NULL || file->kind != QP_FILE_SOURCE ||
      file->artifact != QP_ARTIFACT_MAIN || file->path == NULL) {
    return SQLITE_OK;
  }
  memset(&unused_identity, 0, sizeof(unused_identity));
  rc = qp_append_suffix(file->path, "-journal", &path);
  if (rc != SQLITE_OK) {
    int failure_kind = rc == SQLITE_NOMEM ? QP_FAILURE_IO
                                          : QP_FAILURE_UNSUPPORTED;
    qp_record_failure(failure_kind, QP_ARTIFACT_JOURNAL,
                      "journal_path", rc);
    return qp_latch_file_failure(file, rc);
  }
  path_state = qp_current_path_identity(path, &unused_identity,
                                        QP_ARTIFACT_JOURNAL);
  qp_api->free(path);
  if (path_state == QP_PATH_IDENTITY_MISSING) return SQLITE_OK;
  if (path_state == QP_PATH_IDENTITY_IO) {
    qp_record_failure(QP_FAILURE_IO, QP_ARTIFACT_JOURNAL,
                      "journal_inspect", SQLITE_IOERR);
    return qp_latch_file_failure(file, SQLITE_IOERR);
  }
  /* Any present regular journal, symlink/reparse point, directory, or other
   * unsafe object makes the source family ambiguous.  This is a policy/type
   * rejection, never identity evidence for SourceChanged. */
  qp_record_failure(QP_FAILURE_UNSUPPORTED, QP_ARTIFACT_JOURNAL,
                    "journal_appeared", SQLITE_CANTOPEN);
  return qp_latch_file_failure(file, SQLITE_CANTOPEN);
}

static int qp_path_has_suffix(const char *path, const char *main_path,
                              const char *suffix) {
  size_t main_length = strlen(main_path);
  size_t suffix_length = strlen(suffix);
  return strlen(path) == main_length + suffix_length &&
         memcmp(path, main_path, main_length) == 0 &&
         memcmp(path + main_length, suffix, suffix_length + 1) == 0;
}

#ifdef _WIN32
static int qp_test_marker_remove(const char *path) {
  wchar_t *wide = qp_utf8_to_wide(path);
  int result;
  DWORD error;
  if (wide == NULL) return 0;
  result = DeleteFileW(wide) != 0;
  error = result ? ERROR_SUCCESS : GetLastError();
  qp_api->free(wide);
  return result || error == ERROR_FILE_NOT_FOUND ||
         error == ERROR_PATH_NOT_FOUND;
}

static int qp_test_marker_create(const char *path) {
  wchar_t *wide = qp_utf8_to_wide(path);
  HANDLE handle;
  if (wide == NULL) return 0;
  handle = CreateFileW(wide, GENERIC_WRITE,
                       FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                       NULL, CREATE_NEW,
                       FILE_ATTRIBUTE_TEMPORARY | FILE_FLAG_OPEN_REPARSE_POINT,
                       NULL);
  qp_api->free(wide);
  if (!qp_proof_handle_is_valid(handle)) return 0;
  if (!CloseHandle(handle)) {
    qp_record_base_close_error(QP_ARTIFACT_TEMP, "race_marker_close",
                               SQLITE_IOERR);
    return 0;
  }
  return 1;
}

static int qp_test_marker_exists(const char *path) {
  wchar_t *wide = qp_utf8_to_wide(path);
  DWORD attributes;
  if (wide == NULL) return 0;
  attributes = GetFileAttributesW(wide);
  qp_api->free(wide);
  return attributes != INVALID_FILE_ATTRIBUTES &&
         !(attributes & (FILE_ATTRIBUTE_DIRECTORY |
                         FILE_ATTRIBUTE_REPARSE_POINT));
}
#else
static int qp_test_marker_remove(const char *path) {
  return unlink(path) == 0 || errno == ENOENT;
}

static int qp_test_marker_create(const char *path) {
  int descriptor = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC |
                                  O_NOFOLLOW,
                        0600);
  if (descriptor < 0) return 0;
  if (close(descriptor) != 0) {
    qp_record_base_close_error(QP_ARTIFACT_TEMP, "race_marker_close",
                               SQLITE_IOERR);
    return 0;
  }
  return 1;
}

static int qp_test_marker_exists(const char *path) {
  struct stat status;
  return lstat(path, &status) == 0 && S_ISREG(status.st_mode) &&
         !S_ISLNK(status.st_mode);
}
#endif

/* Private deterministic race hook.  The URI parameter is only emitted by
 * TrustedLiveReader's private test plumbing and is bound to the unguessable
 * session token.  Two barriers let a test install B before the delegated OS
 * open, then restore A before the native identity comparison (A->B->A). */
static int qp_test_race_barrier(int race_artifact, const char *phase) {
  char ready[QP_PATH_CAP];
  char release[QP_PATH_CAP];
  char temp_path[QP_PATH_CAP];
  char token[QP_TOKEN_CAP];
  const char *artifact = qp_artifact_name(race_artifact);
  int enabled;
  int ready_length;
  int release_length;
  int attempts;
  qp_lock();
  enabled = qp_state.configured &&
            strcmp(qp_state.race_artifact, artifact) == 0 &&
            qp_copy_string(temp_path, sizeof(temp_path), qp_state.temp_path) &&
            qp_copy_string(token, sizeof(token), qp_state.token);
  qp_unlock();
  if (!enabled) return SQLITE_OK;
  ready_length = snprintf(ready, sizeof(ready),
                          "%s%cqplot-%s-race-%s-%s-ready.tmp", temp_path,
                          QP_PATH_SEPARATOR, token, artifact, phase);
  release_length = snprintf(release, sizeof(release),
                            "%s%cqplot-%s-race-%s-%s-release.tmp", temp_path,
                            QP_PATH_SEPARATOR, token, artifact, phase);
  if (ready_length <= 0 || ready_length >= (int)sizeof(ready) ||
      release_length <= 0 || release_length >= (int)sizeof(release)) {
    qp_record_failure(QP_FAILURE_POLICY, race_artifact, "race_path",
                      SQLITE_CANTOPEN);
    return SQLITE_CANTOPEN;
  }
  if (!qp_test_marker_remove(ready) || !qp_test_marker_remove(release)) {
    qp_lock();
    qp_state.cleanup_failed = 1;
    qp_set_failure_locked(QP_FAILURE_IO, QP_ARTIFACT_TEMP,
                          "race_marker_delete", SQLITE_IOERR);
    qp_unlock();
    return SQLITE_IOERR;
  }
  if (!qp_test_marker_create(ready)) {
    qp_record_failure(QP_FAILURE_IO, race_artifact, "race_marker",
                      SQLITE_IOERR);
    return SQLITE_IOERR;
  }
  for (attempts = 0; attempts < 1000; attempts++) {
    if (qp_test_marker_exists(release)) break;
    (void)qp_base_vfs->xSleep(qp_base_vfs, 10000);
  }
  if (!qp_test_marker_remove(ready) || !qp_test_marker_remove(release)) {
    qp_lock();
    qp_state.cleanup_failed = 1;
    qp_set_failure_locked(QP_FAILURE_IO, QP_ARTIFACT_TEMP,
                          "race_marker_delete", SQLITE_IOERR);
    qp_unlock();
    return SQLITE_IOERR;
  }
  if (attempts == 1000) {
    qp_record_failure(QP_FAILURE_IO, race_artifact, "race_timeout",
                      SQLITE_INTERRUPT);
    return SQLITE_INTERRUPT;
  }
  return SQLITE_OK;
}

static int qp_is_source_family_locked(const char *path) {
  return qp_state.configured && path != NULL &&
         (strcmp(path, qp_state.main_path) == 0 ||
          qp_path_has_suffix(path, qp_state.main_path, "-wal") ||
          qp_path_has_suffix(path, qp_state.main_path, "-shm") ||
          qp_path_has_suffix(path, qp_state.main_path, "-journal"));
}

static int qp_is_temp_path_locked(const char *path) {
  size_t root_length;
  const char *filename;
  size_t token_length;
  if (!qp_state.configured || path == NULL) {
    return 0;
  }
  root_length = strlen(qp_state.temp_path);
  if (strncmp(path, qp_state.temp_path, root_length) != 0) {
    return 0;
  }
  filename = path + root_length;
  if (*filename == '/' || *filename == '\\') {
    filename++;
  } else if (root_length == 0 ||
             (qp_state.temp_path[root_length - 1] != '/' &&
              qp_state.temp_path[root_length - 1] != '\\')) {
    return 0;
  }
  if (strncmp(filename, "qplot-", 6) != 0) {
    return 0;
  }
  filename += 6;
  token_length = strlen(qp_state.token);
  if (strncmp(filename, qp_state.token, token_length) != 0 ||
      filename[token_length] != '-') {
    return 0;
  }
  filename += token_length + 1;
  if (*filename == '\0' || strchr(filename, '/') != NULL ||
      strchr(filename, '\\') != NULL || strstr(filename, "..") != NULL) {
    return 0;
  }
  return strlen(filename) > 4 &&
         strcmp(filename + strlen(filename) - 4, ".tmp") == 0;
}

static int qp_file_is_current(QpFile *file) {
  int result;
  qp_lock();
  result = qp_state.configured &&
           file->generation == qp_state.generation;
  if (!result && qp_state.configured) {
    qp_state.audit.stale_callback_rejected++;
  }
  qp_unlock();
  return result;
}

static void qp_release_file_reference(QpFile *file) {
  if (!file->counted_ref) {
    return;
  }
  qp_lock();
  if (qp_state.active_files > 0) {
    qp_state.active_files--;
  } else {
    qp_state.cleanup_failed = 1;
    qp_set_failure_locked(QP_FAILURE_IO, file->artifact,
                          "file_counter_underflow", SQLITE_IOERR);
  }
  qp_unlock();
  file->counted_ref = 0;
}

static int qp_reserve_main(const char *name, const char *token,
                           const char *temp_path, const char *race_artifact,
                           QpFile *file) {
  const char *cleanup_fault =
      qp_api->uri_parameter(name, "qplot_test_cleanup_fault");
  int matches;
  int expected_wal_present;
  int expected_shm_present;
  QpU64 expected_wal_a;
  QpU64 expected_wal_b;
  QpU64 expected_shm_a;
  QpU64 expected_shm_b;
  int valid_race = race_artifact == NULL || race_artifact[0] == '\0' ||
                   strcmp(race_artifact, "main") == 0 ||
                   strcmp(race_artifact, "wal") == 0 ||
                   strcmp(race_artifact, "shm") == 0;
  int valid_cleanup_fault =
      cleanup_fault == NULL || cleanup_fault[0] == '\0' ||
      strcmp(cleanup_fault, "proof_close") == 0 ||
      strcmp(cleanup_fault, "shm_unmap") == 0 ||
      strcmp(cleanup_fault, "base_close") == 0;
  if (!qp_parse_optional_expected_identity(
          name, "qplot_expected_wal_kind", "qplot_expected_wal_a",
          "qplot_expected_wal_b", &expected_wal_present, &expected_wal_a,
          &expected_wal_b) ||
      !qp_parse_optional_expected_identity(
          name, "qplot_expected_shm_kind", "qplot_expected_shm_a",
          "qplot_expected_shm_b", &expected_shm_present, &expected_shm_a,
          &expected_shm_b) ||
      !qp_token_is_valid(token) || !qp_validate_existing_path(name, 0) ||
      !qp_validate_existing_path(temp_path, 1) || !valid_race ||
      !valid_cleanup_fault) {
    return SQLITE_CANTOPEN;
  }
  if (strlen(temp_path) + strlen(token) + 64 >= (size_t)qp_base_vfs->mxPathname) {
    return SQLITE_CANTOPEN;
  }
  qp_lock();
  if (!qp_state.configured) {
    QpU64 generation = qp_state.generation + 1;
    if (generation == 0) generation = 1;
    memset(&qp_state, 0, sizeof(qp_state));
    qp_state.generation = generation;
    if (!qp_copy_string(qp_state.token, sizeof(qp_state.token), token) ||
        !qp_copy_string(qp_state.main_path, sizeof(qp_state.main_path), name) ||
        !qp_copy_string(qp_state.temp_path, sizeof(qp_state.temp_path),
                        temp_path)) {
      memset(&qp_state, 0, sizeof(qp_state));
      qp_state.generation = generation;
      qp_unlock();
      return SQLITE_CANTOPEN;
    }
    if (race_artifact != NULL && race_artifact[0] != '\0') {
      if (!qp_copy_string(qp_state.race_artifact,
                          sizeof(qp_state.race_artifact), race_artifact)) {
        memset(&qp_state, 0, sizeof(qp_state));
        qp_state.generation = generation;
        qp_unlock();
        return SQLITE_CANTOPEN;
      }
    }
    if (cleanup_fault != NULL && cleanup_fault[0] != '\0') {
      if (!qp_copy_string(qp_state.cleanup_fault,
                          sizeof(qp_state.cleanup_fault), cleanup_fault)) {
        memset(&qp_state, 0, sizeof(qp_state));
        qp_state.generation = generation;
        qp_unlock();
        return SQLITE_CANTOPEN;
      }
    }
    qp_state.configured = 1;
    qp_state.configured_wal_present = expected_wal_present;
    qp_state.configured_wal_a = expected_wal_a;
    qp_state.configured_wal_b = expected_wal_b;
    qp_state.configured_shm_present = expected_shm_present;
    qp_state.configured_shm_a = expected_shm_a;
    qp_state.configured_shm_b = expected_shm_b;
    qp_state.expected_wal_present = expected_wal_present;
    qp_state.expected_wal_pending = 1;
    qp_state.expected_wal_a = expected_wal_a;
    qp_state.expected_wal_b = expected_wal_b;
    qp_state.expected_shm_present = expected_shm_present;
    qp_state.expected_shm_pending = 1;
    qp_state.expected_shm_a = expected_shm_a;
    qp_state.expected_shm_b = expected_shm_b;
    qp_set_failure_locked(QP_FAILURE_NONE, QP_ARTIFACT_NONE, "none",
                          SQLITE_OK);
  }
  matches = strcmp(qp_state.token, token) == 0 &&
            strcmp(qp_state.main_path, name) == 0 &&
            strcmp(qp_state.temp_path, temp_path) == 0 &&
            qp_state.configured_wal_present == expected_wal_present &&
            qp_state.configured_wal_a == expected_wal_a &&
            qp_state.configured_wal_b == expected_wal_b &&
            qp_state.configured_shm_present == expected_shm_present &&
            qp_state.configured_shm_a == expected_shm_a &&
            qp_state.configured_shm_b == expected_shm_b &&
            ((race_artifact == NULL || race_artifact[0] == '\0')
                 ? qp_state.race_artifact[0] == '\0'
                 : strcmp(qp_state.race_artifact, race_artifact) == 0) &&
            ((cleanup_fault == NULL || cleanup_fault[0] == '\0')
                 ? qp_state.cleanup_fault[0] == '\0'
                 : strcmp(qp_state.cleanup_fault, cleanup_fault) == 0);
  if (matches && !qp_state.cleanup_failed && !qp_state.session_claimed) {
    qp_state.session_claimed = 1;
    qp_state.active_files++;
    file->generation = qp_state.generation;
    file->counted_ref = 1;
  }
  qp_unlock();
  return matches && file->counted_ref ? SQLITE_OK : SQLITE_BUSY;
}

static int qp_reserve_derived(const char *name, const char *token,
                              int flags, QpFile *file) {
  int matches = 0;
  if (!qp_path_is_absolute(name)) return SQLITE_CANTOPEN;
  qp_lock();
  if (qp_state.configured && !qp_state.cleanup_failed && token != NULL &&
      strcmp(token, qp_state.token) == 0) {
    if ((flags & SQLITE_OPEN_WAL) &&
        qp_path_has_suffix(name, qp_state.main_path, "-wal")) {
      matches = 1;
    } else if ((flags & SQLITE_OPEN_MAIN_JOURNAL) &&
               qp_path_has_suffix(name, qp_state.main_path, "-journal")) {
      matches = 1;
    }
  }
  if (matches) {
    qp_state.active_files++;
    file->generation = qp_state.generation;
    file->counted_ref = 1;
  }
  qp_unlock();
  return matches ? SQLITE_OK : SQLITE_CANTOPEN;
}

static int qp_reserve_temp(QpFile *file, char **generated_path) {
  unsigned char random_bytes[8];
  char random_hex[17];
  char candidate[QP_PATH_CAP];
  const char digits[] = "0123456789abcdef";
  QpU64 sequence;
  size_t index;
  int written;
  qp_base_vfs->xRandomness(qp_base_vfs, (int)sizeof(random_bytes),
                           (char *)random_bytes);
  for (index = 0; index < sizeof(random_bytes); index++) {
    random_hex[index * 2] = digits[random_bytes[index] >> 4];
    random_hex[index * 2 + 1] = digits[random_bytes[index] & 15];
  }
  random_hex[16] = '\0';

  qp_lock();
  if (!qp_state.configured || qp_state.cleanup_failed) {
    qp_unlock();
    return SQLITE_CANTOPEN;
  }
  sequence = ++qp_state.temp_sequence;
  written = snprintf(candidate, sizeof(candidate), "%s%cqplot-%s-%016llx-%s.tmp",
                     qp_state.temp_path, QP_PATH_SEPARATOR, qp_state.token,
                     sequence, random_hex);
  if (written <= 0 || written >= (int)sizeof(candidate) ||
      written >= qp_base_vfs->mxPathname) {
    qp_unlock();
    return SQLITE_CANTOPEN;
  }
  qp_state.active_files++;
  file->generation = qp_state.generation;
  file->counted_ref = 1;
  qp_unlock();
  *generated_path = qp_strdup(candidate);
  if (*generated_path == NULL) {
    qp_release_file_reference(file);
    return SQLITE_NOMEM;
  }
  return SQLITE_OK;
}

static int qp_generate_private_temp_path(char **generated_path) {
  unsigned char random_bytes[8];
  char random_hex[17];
  char candidate[QP_PATH_CAP];
  const char digits[] = "0123456789abcdef";
  QpU64 sequence;
  size_t index;
  int written;
  qp_base_vfs->xRandomness(qp_base_vfs, (int)sizeof(random_bytes),
                           (char *)random_bytes);
  for (index = 0; index < sizeof(random_bytes); index++) {
    random_hex[index * 2] = digits[random_bytes[index] >> 4];
    random_hex[index * 2 + 1] = digits[random_bytes[index] & 15];
  }
  random_hex[16] = '\0';
  qp_lock();
  if (!qp_state.configured) {
    qp_unlock();
    return SQLITE_CANTOPEN;
  }
  sequence = ++qp_state.temp_sequence;
  written = snprintf(candidate, sizeof(candidate),
                     "%s%cqplot-%s-wal-%016llx-%s.tmp", qp_state.temp_path,
                     QP_PATH_SEPARATOR, qp_state.token, sequence, random_hex);
  qp_unlock();
  if (written <= 0 || written >= (int)sizeof(candidate) ||
      written >= qp_base_vfs->mxPathname) {
    return SQLITE_CANTOPEN;
  }
  *generated_path = qp_strdup(candidate);
  return *generated_path == NULL ? SQLITE_NOMEM : SQLITE_OK;
}

#ifdef _WIN32
static int qp_create_empty_private_file(const char *path, int *created_out) {
  wchar_t *wide = qp_utf8_to_wide(path);
  HANDLE handle;
  *created_out = 0;
  if (wide == NULL) return SQLITE_NOMEM;
  handle = CreateFileW(wide, GENERIC_READ | GENERIC_WRITE,
                       FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                       NULL, CREATE_NEW,
                       FILE_ATTRIBUTE_TEMPORARY | FILE_FLAG_OPEN_REPARSE_POINT,
                       NULL);
  qp_api->free(wide);
  if (!qp_proof_handle_is_valid(handle)) return SQLITE_CANTOPEN;
  *created_out = 1;
  if (!CloseHandle(handle)) {
    qp_record_base_close_error(QP_ARTIFACT_TEMP,
                               "wal_placeholder_create_close",
                               SQLITE_IOERR);
    return SQLITE_IOERR;
  }
  return SQLITE_OK;
}
#else
static int qp_create_empty_private_file(const char *path, int *created_out) {
  int descriptor = open(path, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC |
                                  O_NOFOLLOW,
                        0600);
  *created_out = 0;
  if (descriptor < 0) return SQLITE_CANTOPEN;
  *created_out = 1;
  if (close(descriptor) != 0) {
    qp_record_base_close_error(QP_ARTIFACT_TEMP,
                               "wal_placeholder_create_close",
                               SQLITE_IOERR);
    return SQLITE_IOERR;
  }
  return SQLITE_OK;
}
#endif

static int qp_delete_placeholder(QpFile *file) {
  int rc = SQLITE_OK;
  if (file->placeholder_path == NULL) return SQLITE_OK;
  if (qp_base_vfs->xDelete(qp_base_vfs, file->placeholder_path, 0) ==
      SQLITE_OK) {
    QP_AUDIT_INC(temp_delete);
  } else {
    qp_lock();
    qp_state.cleanup_failed = 1;
    qp_set_failure_locked(QP_FAILURE_IO, QP_ARTIFACT_TEMP,
                          "wal_placeholder_delete", SQLITE_IOERR);
    qp_unlock();
    rc = SQLITE_IOERR;
  }
  qp_api->free(file->placeholder_path);
  file->placeholder_path = NULL;
  file->wal_placeholder = 0;
  if (rc != SQLITE_OK) (void)qp_latch_file_failure(file, rc);
  return rc;
}

static int qp_source_path_state(QpFile *file) {
  QpIdentity unused_identity;
  return qp_current_path_identity(file->path, &unused_identity,
                                  file->artifact);
}

static int qp_create_wal_placeholder(QpFile *file) {
  int rc = qp_generate_private_temp_path(&file->placeholder_path);
  int created = 0;
  if (rc != SQLITE_OK) return rc;
  rc = qp_create_empty_private_file(file->placeholder_path, &created);
  if (rc != SQLITE_OK) {
    if (created) {
      int delete_rc = qp_delete_placeholder(file);
      return delete_rc == SQLITE_OK ? rc : delete_rc;
    }
    qp_api->free(file->placeholder_path);
    file->placeholder_path = NULL;
    return rc;
  }
  rc = qp_open_private_temp_proof(file->placeholder_path, &file->proof_handle,
                                  &file->proof_identity);
  if (rc != SQLITE_OK) {
    int delete_rc = qp_delete_placeholder(file);
    return delete_rc == SQLITE_OK ? rc : delete_rc;
  }
  file->source_proved = 1;
  file->wal_placeholder = 1;
  QP_AUDIT_INC(temp_redirect);
  return SQLITE_OK;
}

static int qp_promote_wal_if_present(QpFile *file) {
#ifdef _WIN32
  HANDLE new_proof = INVALID_HANDLE_VALUE;
#else
  int new_proof = -1;
#endif
  QpIdentity new_identity;
  QpIdentity path_identity;
  int local_out_flags = 0;
  int state;
  int rc;
  memset(&new_identity, 0, sizeof(new_identity));
  memset(&path_identity, 0, sizeof(path_identity));
  if (file == NULL || !file->wal_placeholder) return SQLITE_OK;
  state = qp_source_path_state(file);
  if (state == 0) return SQLITE_OK;
  if (state < 0) {
    int failure_kind = qp_failure_kind_for_path_state(state, QP_FAILURE_IO);
    int failure_rc = failure_kind == QP_FAILURE_IO ? SQLITE_IOERR
                                                   : SQLITE_CANTOPEN;
    qp_record_failure(failure_kind, QP_ARTIFACT_WAL, "wal_appearance",
                      failure_rc);
    return failure_rc;
  }
  rc = qp_open_proof_handle(file->path, &new_proof, &new_identity,
                            QP_ARTIFACT_WAL);
  if (rc != SQLITE_OK) {
    int current_state = qp_source_path_state(file);
    int failure_kind =
        qp_failure_kind_for_path_state(current_state, QP_FAILURE_IO);
    qp_record_failure(failure_kind, QP_ARTIFACT_WAL,
                      "wal_promotion_proof", rc);
    return rc;
  }
  if (!qp_accept_expected_sidecar_identity(QP_ARTIFACT_WAL, &new_identity)) {
    return qp_close_proof_handle(&new_proof, QP_ARTIFACT_WAL,
                                 "wal_candidate_proof_close")
               ? SQLITE_CANTOPEN
               : SQLITE_IOERR;
  }
  {
    int failure_kind = QP_FAILURE_IO;
    rc = qp_validate_wal_proof_header(new_proof, &failure_kind);
    if (rc != SQLITE_OK) {
      qp_record_failure(failure_kind, QP_ARTIFACT_WAL, "wal_header", rc);
      return qp_close_proof_handle(&new_proof, QP_ARTIFACT_WAL,
                                   "wal_candidate_proof_close")
                 ? rc
                 : SQLITE_IOERR;
    }
  }
  rc = qp_test_race_barrier(file->artifact, "proof");
  if (rc != SQLITE_OK) {
    return qp_close_proof_handle(&new_proof, QP_ARTIFACT_WAL,
                                 "wal_candidate_proof_close")
               ? rc
               : SQLITE_IOERR;
  }
  rc = file->real->pMethods->xClose(file->real);
  if (rc == SQLITE_OK && qp_take_test_cleanup_fault("base_close")) {
    file->real->pMethods = NULL;
    rc = SQLITE_IOERR;
  }
  if (rc != SQLITE_OK) {
    int proof_close_ok = qp_close_proof_handle(
        &new_proof, QP_ARTIFACT_WAL, "wal_candidate_proof_close");
    qp_record_base_close_error(QP_ARTIFACT_WAL,
                               "wal_placeholder_close", rc);
    return proof_close_ok ? rc : SQLITE_IOERR;
  }
  file->real->pMethods = NULL;
  rc = qp_close_source_proof(file);
  if (rc != SQLITE_OK) {
    int candidate_close_ok = qp_close_proof_handle(
        &new_proof, QP_ARTIFACT_WAL, "wal_candidate_proof_close");
    int delete_rc = qp_delete_placeholder(file);
    if (!candidate_close_ok || delete_rc != SQLITE_OK) return SQLITE_IOERR;
    return rc;
  }
  rc = qp_delete_placeholder(file);
  if (rc != SQLITE_OK) {
    return qp_close_proof_handle(&new_proof, QP_ARTIFACT_WAL,
                                 "wal_candidate_proof_close")
               ? rc
               : SQLITE_IOERR;
  }
  file->proof_handle = new_proof;
  file->proof_identity = new_identity;
  file->source_proved = 1;
  memset(file->real, 0, (size_t)qp_base_vfs->szOsFile);
  rc = qp_base_vfs->xOpen(
      qp_base_vfs, file->path, file->real,
      SQLITE_OPEN_READONLY | SQLITE_OPEN_WAL | SQLITE_OPEN_NOFOLLOW,
      &local_out_flags);
  if (rc != SQLITE_OK || file->real->pMethods == NULL) {
    int result_rc = rc == SQLITE_OK ? SQLITE_CANTOPEN : rc;
    int close_rc = SQLITE_OK;
    int proof_rc = SQLITE_OK;
    int base_closed = file->real->pMethods == NULL;
    int path_state = qp_current_path_identity(file->path, &path_identity,
                                              QP_ARTIFACT_WAL);
    if (path_state != QP_PATH_IDENTITY_PRESENT ||
        !qp_identities_equal(&path_identity, &file->proof_identity)) {
      int sticky_rc = qp_file_sticky_failure(file);
      if (sticky_rc != SQLITE_OK) {
        result_rc = sticky_rc;
      } else if (path_state == QP_PATH_IDENTITY_UNSAFE) {
        result_rc = SQLITE_CANTOPEN;
        qp_record_failure(QP_FAILURE_UNSUPPORTED, QP_ARTIFACT_WAL,
                          "wal_promotion_path", result_rc);
      } else if (path_state == QP_PATH_IDENTITY_IO) {
        result_rc = SQLITE_IOERR;
        qp_record_failure(QP_FAILURE_IO, QP_ARTIFACT_WAL,
                          "wal_promotion_path", result_rc);
      } else {
        qp_record_failure(QP_FAILURE_SOURCE_CHANGED, QP_ARTIFACT_WAL,
                          "wal_promotion_path", result_rc);
      }
    } else {
      qp_record_failure(QP_FAILURE_IO, QP_ARTIFACT_WAL,
                        "wal_promotion_open", result_rc);
    }
    if (file->real->pMethods != NULL) {
      close_rc = file->real->pMethods->xClose(file->real);
      if (close_rc == SQLITE_OK) {
        base_closed = 1;
        file->real->pMethods = NULL;
        if (qp_take_test_cleanup_fault("base_close")) close_rc = SQLITE_IOERR;
      }
      if (close_rc != SQLITE_OK) {
        qp_record_base_close_error(QP_ARTIFACT_WAL,
                                   "wal_promotion_partial_close", close_rc);
      } else {
        file->real->pMethods = NULL;
      }
    }
    if (base_closed) proof_rc = qp_close_source_proof(file);
    if (close_rc != SQLITE_OK) result_rc = close_rc;
    if (proof_rc != SQLITE_OK) result_rc = proof_rc;
    (void)qp_latch_file_failure(file, result_rc);
    return result_rc;
  }
  QP_AUDIT_INC(source_open_readonly);
  rc = qp_test_race_barrier(file->artifact, "actual");
  if (rc == SQLITE_OK) {
    rc = qp_validate_source_binding(file, "wal_promotion_actual");
  }
  if (rc == SQLITE_OK &&
      ((local_out_flags & SQLITE_OPEN_READWRITE) != 0 ||
       (local_out_flags & SQLITE_OPEN_READONLY) == 0)) {
    if (local_out_flags & SQLITE_OPEN_READWRITE) {
      QP_AUDIT_INC(source_open_readwrite);
    }
    qp_record_failure(QP_FAILURE_POLICY, QP_ARTIFACT_WAL,
                      "wal_promotion_mode", SQLITE_READONLY);
    rc = SQLITE_READONLY;
  }
  if (rc != SQLITE_OK) (void)qp_latch_file_failure(file, rc);
  return rc;
}

/* Uses QpPathIdentityState, with PRESENT additionally requiring link count 1
 * because the SHM exception must never mutate an aliased file. */
static int qp_shm_path_state(QpFile *file) {
  char *path = NULL;
  int rc = qp_append_suffix(file->path, "-shm", &path);
  int result;
  QpIdentity unused_identity;
  if (rc != SQLITE_OK) return QP_PATH_IDENTITY_IO;
  result = qp_current_path_identity(path, &unused_identity,
                                    QP_ARTIFACT_SHM);
  if (result != QP_PATH_IDENTITY_PRESENT) {
    qp_api->free(path);
    return result;
  }
#ifdef _WIN32
  {
    wchar_t *wide = qp_utf8_to_wide(path);
    HANDLE path_handle = INVALID_HANDLE_VALUE;
    if (wide == NULL) {
      qp_api->free(path);
      return QP_PATH_IDENTITY_IO;
    }
    path_handle = CreateFileW(
        wide, FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT, NULL);
    if (!qp_proof_handle_is_valid(path_handle)) {
      DWORD error = GetLastError();
      result = error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND
                   ? QP_PATH_IDENTITY_MISSING
                   : QP_PATH_IDENTITY_IO;
    } else {
      qp_note_proof_open();
      result = qp_handle_link_state(path_handle);
      if (!qp_close_proof_handle(&path_handle, QP_ARTIFACT_SHM,
                                 "shm_path_close")) {
        result = QP_PATH_IDENTITY_IO;
      }
    }
    qp_api->free(wide);
  }
#else
  {
    struct stat status;
    if (lstat(path, &status) != 0) {
      result = errno == ENOENT || errno == ENOTDIR
                   ? QP_PATH_IDENTITY_MISSING
                   : QP_PATH_IDENTITY_IO;
    } else if (status.st_nlink != 1) {
      result = QP_PATH_IDENTITY_ALIAS;
    }
  }
#endif
  qp_api->free(path);
  return result;
}

static int qp_get_actual_shm_identity(QpFile *file, QpIdentity *identity,
                                      int *failure_kind) {
  char *expected_path = NULL;
  int rc = qp_append_suffix(file->path, "-shm", &expected_path);
  if (rc != SQLITE_OK) {
    *failure_kind = QP_FAILURE_POLICY;
    return rc;
  }
  memset(identity, 0, sizeof(*identity));
#ifdef _WIN32
  {
    QpPinnedWinFile *database = (QpPinnedWinFile *)file->real;
    QpPinnedWinShm *shm = database->pShm;
    QpPinnedWinShmNode *node;
    QpIdentity shared_identity;
    QpIdentity lock_identity;
    memset(&shared_identity, 0, sizeof(shared_identity));
    memset(&lock_identity, 0, sizeof(lock_identity));
    if (shm == NULL) {
      qp_api->free(expected_path);
      *failure_kind = QP_FAILURE_NONE;
      return SQLITE_NOTFOUND;
    }
    node = shm->pShmNode;
    if (node == NULL || node->zFilename == NULL ||
        _stricmp(node->zFilename, expected_path) != 0 ||
        node->bUseSharedLockHandle != 0 ||
        !qp_proof_handle_is_valid(node->hSharedShm) ||
        !qp_proof_handle_is_valid(shm->hShm)) {
      qp_api->free(expected_path);
      *failure_kind = QP_FAILURE_UNSUPPORTED;
      return SQLITE_READONLY_CANTINIT;
    }
    if (node->isReadonly != 0 || shm->bReadonly != 0 ||
        !qp_windows_handle_is_readwrite(node->hSharedShm) ||
        !qp_windows_handle_is_readwrite(shm->hShm)) {
      qp_api->free(expected_path);
      *failure_kind = QP_FAILURE_POLICY;
      return SQLITE_READONLY_CANTINIT;
    }
    if (!qp_identity_from_handle(node->hSharedShm, &shared_identity) ||
        !qp_identity_from_handle(shm->hShm, &lock_identity)) {
      qp_api->free(expected_path);
      *failure_kind = QP_FAILURE_IO;
      return SQLITE_IOERR_FSTAT;
    }
    {
      int shared_link_state = qp_handle_link_state(node->hSharedShm);
      int lock_link_state = qp_handle_link_state(shm->hShm);
      if (shared_link_state == QP_PATH_IDENTITY_IO ||
          lock_link_state == QP_PATH_IDENTITY_IO) {
        qp_api->free(expected_path);
        *failure_kind = QP_FAILURE_IO;
        return SQLITE_IOERR_FSTAT;
      }
      if (shared_link_state == QP_PATH_IDENTITY_ALIAS ||
          lock_link_state == QP_PATH_IDENTITY_ALIAS) {
        qp_api->free(expected_path);
        *failure_kind = QP_FAILURE_UNSUPPORTED;
        return SQLITE_READONLY_CANTINIT;
      }
    }
    if (!qp_identities_equal(&shared_identity, &lock_identity)) {
      qp_api->free(expected_path);
      *failure_kind = QP_FAILURE_SOURCE_CHANGED;
      return SQLITE_IOERR;
    }
    *identity = shared_identity;
  }
#else
  {
    QpPinnedUnixFile *database = (QpPinnedUnixFile *)file->real;
    QpPinnedUnixShm *shm = database->pShm;
    QpPinnedUnixShmNode *node;
    int open_flags;
    if (shm == NULL) {
      qp_api->free(expected_path);
      *failure_kind = QP_FAILURE_NONE;
      return SQLITE_NOTFOUND;
    }
    node = shm->pShmNode;
    if (node == NULL || node->zFilename == NULL ||
        strcmp(node->zFilename, expected_path) != 0 || node->hShm < 0 ||
        node->isReadonly != 0) {
      qp_api->free(expected_path);
      *failure_kind = node != NULL && node->isReadonly != 0
                          ? QP_FAILURE_POLICY
                          : QP_FAILURE_UNSUPPORTED;
      return SQLITE_READONLY_CANTINIT;
    }
    open_flags = fcntl(node->hShm, F_GETFL);
    if (open_flags < 0 ||
        !qp_identity_from_descriptor(node->hShm, identity) ||
        !qp_posix_descriptor_is_local(node->hShm)) {
      qp_api->free(expected_path);
      *failure_kind = QP_FAILURE_IO;
      return SQLITE_IOERR_FSTAT;
    }
    {
      int link_state = qp_handle_link_state(node->hShm);
      if (link_state != QP_PATH_IDENTITY_PRESENT) {
        qp_api->free(expected_path);
        *failure_kind = link_state == QP_PATH_IDENTITY_ALIAS
                            ? QP_FAILURE_UNSUPPORTED
                            : QP_FAILURE_IO;
        return link_state == QP_PATH_IDENTITY_ALIAS
                   ? SQLITE_READONLY_CANTINIT
                   : SQLITE_IOERR_FSTAT;
      }
    }
    if ((open_flags & O_ACCMODE) != O_RDWR) {
      qp_api->free(expected_path);
      *failure_kind = QP_FAILURE_POLICY;
      return SQLITE_READONLY_CANTINIT;
    }
  }
#endif
  qp_api->free(expected_path);
  *failure_kind = QP_FAILURE_NONE;
  return SQLITE_OK;
}

static int qp_validate_shm_binding_now(QpFile *file,
                                       const char *operation) {
  QpIdentity proof_now;
  QpIdentity actual_now;
  QpIdentity path_now;
  char *path = NULL;
  int failure_kind = QP_FAILURE_IO;
  int link_state;
  int path_state;
  int rc;
  memset(&proof_now, 0, sizeof(proof_now));
  memset(&actual_now, 0, sizeof(actual_now));
  memset(&path_now, 0, sizeof(path_now));
  if (!file->shm_proved || !qp_proof_handle_is_valid(file->shm_anchor)) {
    qp_record_failure(QP_FAILURE_UNSUPPORTED, QP_ARTIFACT_SHM, operation,
                      SQLITE_CANTOPEN);
    return SQLITE_CANTOPEN;
  }
  link_state = qp_handle_link_state(file->shm_anchor);
  if (link_state != QP_PATH_IDENTITY_PRESENT) {
    int link_rc = link_state == QP_PATH_IDENTITY_ALIAS
                      ? SQLITE_READONLY_CANTINIT
                      : SQLITE_IOERR_FSTAT;
    qp_record_failure(link_state == QP_PATH_IDENTITY_ALIAS
                          ? QP_FAILURE_UNSUPPORTED
                          : QP_FAILURE_IO,
                      QP_ARTIFACT_SHM,
                      link_state == QP_PATH_IDENTITY_ALIAS
                          ? "shm_hardlink"
                          : "shm_proof_link",
                      link_rc);
    return link_rc;
  }
#ifdef _WIN32
  if (!qp_identity_from_handle(file->shm_anchor, &proof_now)) {
#else
  if (!qp_identity_from_descriptor(file->shm_anchor, &proof_now)) {
#endif
    qp_record_failure(QP_FAILURE_IO, QP_ARTIFACT_SHM, operation,
                      SQLITE_IOERR_FSTAT);
    return SQLITE_IOERR_FSTAT;
  }
  if (!qp_identities_equal(&proof_now, &file->shm_identity)) {
    qp_record_failure(QP_FAILURE_SOURCE_CHANGED, QP_ARTIFACT_SHM, operation,
                      SQLITE_IOERR);
    return SQLITE_IOERR;
  }
  rc = qp_get_actual_shm_identity(file, &actual_now, &failure_kind);
  if (rc != SQLITE_OK) {
    qp_record_failure(failure_kind == QP_FAILURE_NONE ? QP_FAILURE_IO
                                                     : failure_kind,
                      QP_ARTIFACT_SHM, operation,
                      rc == SQLITE_NOTFOUND ? SQLITE_IOERR : rc);
    return rc == SQLITE_NOTFOUND ? SQLITE_IOERR : rc;
  }
  if (!qp_identities_equal(&actual_now, &file->shm_identity)) {
    qp_record_failure(QP_FAILURE_SOURCE_CHANGED, QP_ARTIFACT_SHM, operation,
                      SQLITE_IOERR);
    return SQLITE_IOERR;
  }
  rc = qp_append_suffix(file->path, "-shm", &path);
  if (rc != SQLITE_OK) {
    qp_record_failure(QP_FAILURE_POLICY, QP_ARTIFACT_SHM, operation, rc);
    return rc;
  }
  path_state = qp_current_path_identity(path, &path_now, QP_ARTIFACT_SHM);
  if (path_state != QP_PATH_IDENTITY_PRESENT ||
      !qp_identities_equal(&path_now, &file->shm_identity)) {
    int sticky_rc = qp_file_sticky_failure(file);
    qp_api->free(path);
    if (sticky_rc != SQLITE_OK) return sticky_rc;
    if (path_state == QP_PATH_IDENTITY_UNSAFE) {
      qp_record_failure(QP_FAILURE_UNSUPPORTED, QP_ARTIFACT_SHM, operation,
                        SQLITE_CANTOPEN);
      return SQLITE_CANTOPEN;
    }
    if (path_state == QP_PATH_IDENTITY_IO) {
      qp_record_failure(QP_FAILURE_IO, QP_ARTIFACT_SHM, operation,
                        SQLITE_IOERR);
      return SQLITE_IOERR;
    }
    qp_record_failure(QP_FAILURE_SOURCE_CHANGED, QP_ARTIFACT_SHM, operation,
                      SQLITE_IOERR);
    return SQLITE_IOERR;
  }
  qp_api->free(path);
  QP_AUDIT_INC(identity_verified);
  return SQLITE_OK;
}

static int qp_validate_shm_binding(QpFile *file, const char *operation) {
  int rc = qp_file_sticky_failure(file);
  if (rc != SQLITE_OK) return rc;
  return qp_latch_file_failure(file,
                               qp_validate_shm_binding_now(file, operation));
}

static int qp_close(sqlite3_file *sqlite_file) {
  QpFile *file = (QpFile *)sqlite_file;
  int rc = SQLITE_OK;
  int unmap_rc = SQLITE_OK;
  int shm_proof_rc = SQLITE_OK;
  int source_proof_rc = SQLITE_OK;
  int delete_rc = SQLITE_OK;
  int base_closed = file->real == NULL || file->real->pMethods == NULL;
  /* Remove first so token-gated explicit validation cannot inspect this
   * wrapper while delegated unmap/close mutates its private OS-VFS state. */
  qp_active_file_remove(file);
  if (file->shm_active && file->real != NULL &&
      file->real->pMethods != NULL &&
      file->real->pMethods->xShmUnmap != NULL) {
    QP_AUDIT_INC(shm_unmap);
    unmap_rc = file->real->pMethods->xShmUnmap(file->real, 0);
    if (unmap_rc == SQLITE_OK &&
        qp_take_test_cleanup_fault("shm_unmap")) {
      unmap_rc = SQLITE_IOERR_SHMMAP;
    }
    if (unmap_rc != SQLITE_OK) {
      qp_record_shm_unmap_error("close_shm_unmap", unmap_rc);
    }
  } else if (file->shm_active) {
    unmap_rc = SQLITE_IOERR_SHMMAP;
    qp_record_shm_unmap_error("close_shm_unmap_missing", unmap_rc);
  }
  if (file->real != NULL && file->real->pMethods != NULL) {
    rc = file->real->pMethods->xClose(file->real);
    if (rc == SQLITE_OK) {
      base_closed = 1;
      file->real->pMethods = NULL;
      if (qp_take_test_cleanup_fault("base_close")) {
        rc = SQLITE_IOERR;
        qp_record_base_close_error(file->artifact, "close", rc);
      }
    } else {
      qp_record_base_close_error(file->artifact, "close", rc);
    }
  }
  /* Proof handles close only after the pinned VFS has released its locks.
   * On POSIX, reversing this order would drop every process-owned fcntl lock
   * for the inode.  If the real close failed, retain the proof handles as
   * leak evidence and quarantine the native session instead of perturbing
   * locks possibly still owned by the leaked SQLite descriptor. */
  if (base_closed) {
    shm_proof_rc = qp_clear_shm_proof(file);
    source_proof_rc = qp_close_source_proof(file);
  }
  if (file->wal_placeholder || file->placeholder_path != NULL)
    delete_rc = qp_delete_placeholder(file);
  if (file->path != NULL) {
    qp_api->free(file->path);
    file->path = NULL;
  }
  qp_release_file_reference(file);
  sqlite_file->pMethods = NULL;
  if (unmap_rc != SQLITE_OK) return unmap_rc;
  if (rc != SQLITE_OK) return rc;
  if (shm_proof_rc != SQLITE_OK) return shm_proof_rc;
  if (source_proof_rc != SQLITE_OK) return source_proof_rc;
  return delete_rc;
}

static int qp_before_callback(QpFile *file, const char *operation) {
  int rc;
  if (!qp_file_is_current(file)) return SQLITE_IOERR;
  rc = qp_file_sticky_failure(file);
  if (rc != SQLITE_OK) return rc;
  if (file->wal_placeholder &&
      (strcmp(operation, "read_pre") == 0 ||
       strcmp(operation, "size_pre") == 0)) {
    rc = qp_promote_wal_if_present(file);
    if (rc != SQLITE_OK) return rc;
  }
  if (file->kind == QP_FILE_SOURCE) {
    return qp_validate_source_binding(file, operation);
  }
  return SQLITE_OK;
}

static int qp_after_source_callback(QpFile *file, const char *operation,
                                    int delegated_rc) {
  int validation_rc;
  if (file->kind != QP_FILE_SOURCE) return delegated_rc;
  validation_rc = qp_validate_source_binding(file, operation);
  return validation_rc == SQLITE_OK ? delegated_rc : validation_rc;
}

static int qp_read(sqlite3_file *sqlite_file, void *buffer, int amount,
                   sqlite3_int64 offset) {
  QpFile *file = (QpFile *)sqlite_file;
  int rc = qp_before_callback(file, "read_pre");
  if (rc != SQLITE_OK) return rc;
  if (file->kind == QP_FILE_SOURCE) {
    qp_lock();
    qp_state.audit.source_read++;
    if (amount > 0) qp_state.audit.source_read_bytes += (QpU64)amount;
    qp_unlock();
  }
  rc = file->real->pMethods->xRead(file->real, buffer, amount, offset);
  return qp_after_source_callback(file, "read_post", rc);
}

static int qp_write(sqlite3_file *sqlite_file, const void *buffer, int amount,
                    sqlite3_int64 offset) {
  QpFile *file = (QpFile *)sqlite_file;
  int rc = qp_before_callback(file, "write_pre");
  if (rc != SQLITE_OK) return rc;
  if (file->kind == QP_FILE_SOURCE) {
    QP_AUDIT_INC(source_write);
    qp_record_failure(QP_FAILURE_POLICY, file->artifact, "write",
                      SQLITE_READONLY);
    return SQLITE_READONLY;
  }
  qp_lock();
  qp_state.audit.temp_write++;
  if (amount > 0) qp_state.audit.temp_write_bytes += (QpU64)amount;
  qp_unlock();
  return file->real->pMethods->xWrite(file->real, buffer, amount, offset);
}

static int qp_truncate(sqlite3_file *sqlite_file, sqlite3_int64 size) {
  QpFile *file = (QpFile *)sqlite_file;
  int rc = qp_before_callback(file, "truncate_pre");
  if (rc != SQLITE_OK) return rc;
  if (file->kind == QP_FILE_SOURCE) {
    QP_AUDIT_INC(source_truncate);
    qp_record_failure(QP_FAILURE_POLICY, file->artifact, "truncate",
                      SQLITE_READONLY);
    return SQLITE_READONLY;
  }
  return file->real->pMethods->xTruncate(file->real, size);
}

static int qp_sync(sqlite3_file *sqlite_file, int flags) {
  QpFile *file = (QpFile *)sqlite_file;
  int rc = qp_before_callback(file, "sync_pre");
  if (rc != SQLITE_OK) return rc;
  if (file->kind == QP_FILE_SOURCE) {
    QP_AUDIT_INC(source_sync);
    qp_record_failure(QP_FAILURE_POLICY, file->artifact, "sync",
                      SQLITE_READONLY);
    return SQLITE_READONLY;
  }
  return file->real->pMethods->xSync(file->real, flags);
}

static int qp_file_size(sqlite3_file *sqlite_file, sqlite3_int64 *size) {
  QpFile *file = (QpFile *)sqlite_file;
  int rc = qp_before_callback(file, "size_pre");
  if (rc != SQLITE_OK) return rc;
  rc = file->real->pMethods->xFileSize(file->real, size);
  return qp_after_source_callback(file, "size_post", rc);
}

static int qp_file_lock(sqlite3_file *sqlite_file, int lock) {
  QpFile *file = (QpFile *)sqlite_file;
  int rc = qp_before_callback(file, "lock_pre");
  if (rc != SQLITE_OK) return rc;
  rc = file->real->pMethods->xLock(file->real, lock);
  return qp_after_source_callback(file, "lock_post", rc);
}

static int qp_file_unlock(sqlite3_file *sqlite_file, int lock) {
  QpFile *file = (QpFile *)sqlite_file;
  int rc = qp_before_callback(file, "unlock_pre");
  if (rc != SQLITE_OK) return rc;
  rc = file->real->pMethods->xUnlock(file->real, lock);
  return qp_after_source_callback(file, "unlock_post", rc);
}

static int qp_check_reserved_lock(sqlite3_file *sqlite_file, int *result) {
  QpFile *file = (QpFile *)sqlite_file;
  int rc = qp_before_callback(file, "reserved_pre");
  if (rc != SQLITE_OK) return rc;
  rc = file->real->pMethods->xCheckReservedLock(file->real, result);
  return qp_after_source_callback(file, "reserved_post", rc);
}

static int qp_file_control_is_mutating(int operation) {
  switch (operation) {
    case QP_FCNTL_SIZE_HINT:
    case QP_FCNTL_CHUNK_SIZE:
    case QP_FCNTL_SYNC_OMITTED:
    case QP_FCNTL_PERSIST_WAL:
    case QP_FCNTL_OVERWRITE:
    case QP_FCNTL_POWERSAFE_OVERWRITE:
    case QP_FCNTL_BEGIN_ATOMIC_WRITE:
    case QP_FCNTL_COMMIT_ATOMIC_WRITE:
    case QP_FCNTL_ROLLBACK_ATOMIC_WRITE:
    case QP_FCNTL_CKPT_DONE:
    case QP_FCNTL_RESERVE_BYTES:
    case QP_FCNTL_CKPT_START:
    case QP_FCNTL_CKSM_FILE:
    case QP_FCNTL_RESET_CACHE:
    case QP_FCNTL_NULL_IO:
      return 1;
    default:
      return 0;
  }
}

static int qp_file_control(sqlite3_file *sqlite_file, int operation,
                           void *argument) {
  QpFile *file = (QpFile *)sqlite_file;
  int rc = qp_before_callback(file, "file_control_pre");
  if (rc != SQLITE_OK) return rc;
  if (file->kind == QP_FILE_SOURCE) {
    if (operation == SQLITE_FCNTL_MMAP_SIZE) {
      if (argument != NULL) *((sqlite3_int64 *)argument) = 0;
      return qp_after_source_callback(file, "file_control_post", SQLITE_OK);
    }
    if (qp_file_control_is_mutating(operation)) {
      QP_AUDIT_INC(source_write);
      qp_record_failure(QP_FAILURE_POLICY, file->artifact, "file_control",
                        SQLITE_READONLY);
      return SQLITE_READONLY;
    }
    /* Read-only and diagnostic controls are delegated to the pinned OS VFS;
     * unknown controls are deliberately hidden rather than becoming an
     * unreviewed mutation channel in future SQLite releases. */
    switch (operation) {
      case QP_FCNTL_LOCKSTATE:
      case 4:  /* SQLITE_FCNTL_LAST_ERRNO */
      case 12: /* SQLITE_FCNTL_VFSNAME */
      case 20: /* SQLITE_FCNTL_HAS_MOVED */
      case 34: /* SQLITE_FCNTL_LOCK_TIMEOUT */
      case 35: /* SQLITE_FCNTL_DATA_VERSION */
      case 40: /* SQLITE_FCNTL_EXTERNAL_READER */
        rc = file->real->pMethods->xFileControl(file->real, operation,
                                                 argument);
        return qp_after_source_callback(file, "file_control_post", rc);
      default:
        return SQLITE_NOTFOUND;
    }
  }
  return file->real->pMethods->xFileControl(file->real, operation, argument);
}

static int qp_sector_size(sqlite3_file *sqlite_file) {
  QpFile *file = (QpFile *)sqlite_file;
  int result;
  if (qp_before_callback(file, "sector_pre") != SQLITE_OK) return 0;
  result = file->real->pMethods->xSectorSize(file->real);
  if (file->kind == QP_FILE_SOURCE &&
      qp_validate_source_binding(file, "sector_post") != SQLITE_OK) {
    return 0;
  }
  return result;
}

static int qp_device_characteristics(sqlite3_file *sqlite_file) {
  QpFile *file = (QpFile *)sqlite_file;
  int result;
  if (qp_before_callback(file, "device_pre") != SQLITE_OK) return 0;
  result = file->real->pMethods->xDeviceCharacteristics(file->real);
  if (file->kind == QP_FILE_SOURCE &&
      qp_validate_source_binding(file, "device_post") != SQLITE_OK) {
    return 0;
  }
  return result;
}

static int qp_shm_map(sqlite3_file *sqlite_file, int region, int region_size,
                      int extend, void volatile **mapped) {
  QpFile *file = (QpFile *)sqlite_file;
  QpIdentity unused_identity;
  int failure_kind = QP_FAILURE_NONE;
  int proof_before_delegate = 0;
  int proof_failure_recorded = 0;
  int path_state;
  int validation_rc;
  int rc;
  if (mapped != NULL) *mapped = NULL;
  if (qp_before_callback(file, "shm_map_source_pre") != SQLITE_OK ||
      file->kind != QP_FILE_SOURCE || file->artifact != QP_ARTIFACT_MAIN ||
      mapped == NULL || file->real->pMethods->iVersion < 2 ||
      file->real->pMethods->xShmMap == NULL ||
      file->real->pMethods->xShmUnmap == NULL) {
    QP_AUDIT_INC(shm_map_rejected);
    return SQLITE_READONLY_CANTINIT;
  }
  if (file->shm_active) {
    validation_rc = qp_validate_shm_binding(file, "shm_map_existing_pre");
    if (validation_rc != SQLITE_OK) {
      QP_AUDIT_INC(shm_map_rejected);
      return validation_rc;
    }
  } else {
    path_state = qp_shm_path_state(file);
    if (path_state == 0 &&
        qp_expected_sidecar_requires_present(QP_ARTIFACT_SHM)) {
      QP_AUDIT_INC(shm_map_rejected);
      qp_record_failure(QP_FAILURE_SOURCE_CHANGED, QP_ARTIFACT_SHM,
                        "expected_shm_missing", SQLITE_CANTOPEN);
      return SQLITE_CANTOPEN;
    }
    if (path_state < 0) {
      int sticky_rc = qp_file_sticky_failure(file);
      int path_failure_kind =
          qp_failure_kind_for_path_state(path_state, QP_FAILURE_IO);
      int path_failure_rc = path_failure_kind == QP_FAILURE_IO
                                ? SQLITE_IOERR
                                : SQLITE_CANTOPEN;
      QP_AUDIT_INC(shm_map_rejected);
      if (sticky_rc != SQLITE_OK) return sticky_rc;
      qp_record_failure(path_failure_kind, QP_ARTIFACT_SHM,
                        path_state == QP_PATH_IDENTITY_ALIAS
                            ? "shm_hardlink"
                            : "shm_path",
                        path_failure_rc);
      return path_failure_rc;
    }
    if (path_state > 0) {
      rc = qp_open_shm_proof(file, &proof_failure_recorded);
      if (rc != SQLITE_OK) {
        int current_state = qp_shm_path_state(file);
        QP_AUDIT_INC(shm_map_rejected);
        if (!proof_failure_recorded) {
          int failure_kind = qp_failure_kind_for_path_state(
              current_state, QP_FAILURE_IO);
          qp_record_failure(failure_kind, QP_ARTIFACT_SHM,
                            current_state == QP_PATH_IDENTITY_ALIAS
                                ? "shm_hardlink"
                                : "shm_proof_open",
                            rc);
        }
        return rc;
      }
      proof_before_delegate = 1;
      rc = qp_test_race_barrier(QP_ARTIFACT_SHM, "proof");
      if (rc != SQLITE_OK) {
        int proof_rc;
        QP_AUDIT_INC(shm_map_rejected);
        proof_rc = qp_clear_shm_proof(file);
        return proof_rc == SQLITE_OK ? rc : proof_rc;
      }
    }
  }
  if (extend) QP_AUDIT_INC(shm_map_extend);
  rc = file->real->pMethods->xShmMap(file->real, region, region_size, extend,
                                     mapped);
  validation_rc = qp_get_actual_shm_identity(file, &unused_identity,
                                             &failure_kind);
  if (validation_rc == SQLITE_NOTFOUND) {
    int proof_rc = proof_before_delegate ? qp_clear_shm_proof(file)
                                         : SQLITE_OK;
    if (proof_rc != SQLITE_OK) {
      QP_AUDIT_INC(shm_map_rejected);
      return proof_rc;
    }
    if (rc != SQLITE_OK) {
      QP_AUDIT_INC(shm_map_rejected);
      return rc;
    }
    QP_AUDIT_INC(shm_map_rejected);
    qp_record_failure(QP_FAILURE_UNSUPPORTED, QP_ARTIFACT_SHM,
                      "shm_actual_missing", SQLITE_READONLY_CANTINIT);
    return SQLITE_READONLY_CANTINIT;
  }
  file->shm_active = 1;
  validation_rc = qp_test_race_barrier(QP_ARTIFACT_SHM, "actual");
  if (validation_rc == SQLITE_OK && !file->shm_proved) {
    proof_failure_recorded = 0;
    validation_rc = qp_open_shm_proof(file, &proof_failure_recorded);
    if (validation_rc != SQLITE_OK) {
      int current_state = qp_shm_path_state(file);
      if (!proof_failure_recorded) {
        int failure_kind = qp_failure_kind_for_path_state(
            current_state, QP_FAILURE_IO);
        qp_record_failure(failure_kind, QP_ARTIFACT_SHM,
                          current_state == QP_PATH_IDENTITY_ALIAS
                              ? "shm_hardlink"
                              : "shm_proof_post_open",
                          validation_rc);
      }
    }
  }
  if (validation_rc == SQLITE_OK) {
    validation_rc = qp_validate_shm_binding(file, "shm_map_actual");
  }
  if (validation_rc != SQLITE_OK) {
    int cleanup_rc;
    int cleanup_completed = 0;
    int proof_rc = SQLITE_OK;
    QP_AUDIT_INC(shm_map_rejected);
    QP_AUDIT_INC(shm_unmap);
    cleanup_rc = file->real->pMethods->xShmUnmap(file->real, 0);
    if (cleanup_rc == SQLITE_OK) {
      cleanup_completed = 1;
      if (qp_take_test_cleanup_fault("shm_unmap")) {
        cleanup_rc = SQLITE_IOERR_SHMMAP;
      }
    }
    if (cleanup_rc != SQLITE_OK) {
      qp_record_shm_unmap_error("shm_map_cleanup_unmap", cleanup_rc);
    }
    *mapped = NULL;
    if (cleanup_completed) proof_rc = qp_clear_shm_proof(file);
    if (cleanup_rc != SQLITE_OK) return cleanup_rc;
    if (proof_rc != SQLITE_OK) return proof_rc;
    return validation_rc;
  }
  if (rc == SQLITE_OK && *mapped != NULL) {
    QP_AUDIT_INC(shm_map_writable);
  } else if (rc != SQLITE_OK) {
    QP_AUDIT_INC(shm_map_rejected);
  }
  validation_rc = qp_validate_source_binding(file, "shm_map_source_post");
  return validation_rc == SQLITE_OK ? rc : validation_rc;
}

static int qp_shm_lock(sqlite3_file *sqlite_file, int offset, int count,
                       int flags) {
  QpFile *file = (QpFile *)sqlite_file;
  int rc = qp_before_callback(file, "shm_lock_source_pre");
  if (rc != SQLITE_OK || file->kind != QP_FILE_SOURCE || !file->shm_active ||
      file->real->pMethods->xShmLock == NULL) return SQLITE_IOERR;
  rc = qp_validate_shm_binding(file, "shm_lock_pre");
  if (rc != SQLITE_OK) return rc;
  QP_AUDIT_INC(shm_lock);
  rc = file->real->pMethods->xShmLock(file->real, offset, count, flags);
  {
    int validation_rc = qp_validate_shm_binding(file, "shm_lock_post");
    if (validation_rc != SQLITE_OK) return validation_rc;
  }
  return qp_after_source_callback(file, "shm_lock_source_post", rc);
}

static void qp_shm_barrier(sqlite3_file *sqlite_file) {
  QpFile *file = (QpFile *)sqlite_file;
  int current = qp_file_is_current(file);
  if (current && file->kind == QP_FILE_SOURCE && file->shm_active &&
      file->real->pMethods->xShmBarrier != NULL) {
    (void)qp_validate_source_binding(file, "shm_barrier_source_pre");
    (void)qp_validate_shm_binding(file, "shm_barrier_pre");
    /* Never omit SQLite's required memory fence.  This callback is void, so
     * any validation failure is latched for the next error-returning call. */
    file->real->pMethods->xShmBarrier(file->real);
    (void)qp_validate_shm_binding(file, "shm_barrier_post");
    (void)qp_validate_source_binding(file, "shm_barrier_source_post");
  }
}

static int qp_shm_unmap(sqlite3_file *sqlite_file, int delete_flag) {
  QpFile *file = (QpFile *)sqlite_file;
  int rc = SQLITE_OK;
  int validation_rc = SQLITE_OK;
  int proof_rc = SQLITE_OK;
  int unmap_completed = 0;
  if (delete_flag) QP_AUDIT_INC(shm_unmap_delete_requested);
  if (qp_file_is_current(file) && file->kind == QP_FILE_SOURCE &&
      file->shm_active) {
    validation_rc = qp_validate_shm_binding(file, "shm_unmap_pre");
  }
  if (file->kind == QP_FILE_SOURCE && file->shm_active && file->real != NULL &&
      file->real->pMethods != NULL &&
      file->real->pMethods->xShmUnmap != NULL) {
    QP_AUDIT_INC(shm_unmap);
    rc = file->real->pMethods->xShmUnmap(file->real, 0);
    if (rc == SQLITE_OK) {
      unmap_completed = 1;
      if (qp_take_test_cleanup_fault("shm_unmap")) {
        rc = SQLITE_IOERR_SHMMAP;
      }
    }
    if (rc != SQLITE_OK) {
      qp_record_shm_unmap_error("shm_unmap", rc);
    }
  } else if (file->shm_active) {
    rc = SQLITE_IOERR_SHMMAP;
    qp_record_shm_unmap_error("shm_unmap_missing", rc);
  }
  /* deleteFlag is intentionally never forwarded. */
  if (unmap_completed) proof_rc = qp_clear_shm_proof(file);
  if (rc != SQLITE_OK) return rc;
  if (proof_rc != SQLITE_OK) return proof_rc;
  return validation_rc;
}

static int qp_fetch(sqlite3_file *sqlite_file, sqlite3_int64 offset,
                    int amount, void **mapped) {
  QpFile *file = (QpFile *)sqlite_file;
  (void)offset;
  (void)amount;
  if (qp_before_callback(file, "fetch_pre") != SQLITE_OK) return SQLITE_IOERR;
  if (file->kind == QP_FILE_SOURCE) {
    QP_AUDIT_INC(source_fetch);
    if (mapped != NULL) *mapped = NULL;
    return qp_validate_source_binding(file, "fetch_post");
  }
  if (file->real->pMethods->iVersion >= 3 &&
      file->real->pMethods->xFetch != NULL) {
    return file->real->pMethods->xFetch(file->real, offset, amount, mapped);
  }
  if (mapped != NULL) *mapped = NULL;
  return SQLITE_OK;
}

static int qp_unfetch(sqlite3_file *sqlite_file, sqlite3_int64 offset,
                      void *mapped) {
  QpFile *file = (QpFile *)sqlite_file;
  if (qp_before_callback(file, "unfetch_pre") != SQLITE_OK) return SQLITE_IOERR;
  if (file->kind == QP_FILE_SOURCE) {
    return qp_validate_source_binding(file, "unfetch_post");
  }
  if (file->real->pMethods->iVersion >= 3 &&
      file->real->pMethods->xUnfetch != NULL) {
    return file->real->pMethods->xUnfetch(file->real, offset, mapped);
  }
  return SQLITE_OK;
}

static const sqlite3_io_methods qp_io_methods = {
  3, qp_close, qp_read, qp_write, qp_truncate, qp_sync, qp_file_size,
  qp_file_lock, qp_file_unlock, qp_check_reserved_lock, qp_file_control,
  qp_sector_size, qp_device_characteristics, qp_shm_map, qp_shm_lock,
  qp_shm_barrier, qp_shm_unmap, qp_fetch, qp_unfetch
};

static int qp_partial_open_cleanup(QpFile *file, int rc) {
  int close_rc = SQLITE_OK;
  int shm_proof_rc = SQLITE_OK;
  int source_proof_rc = SQLITE_OK;
  int delete_rc = SQLITE_OK;
  int base_closed = file->real == NULL || file->real->pMethods == NULL;
  if (file->real != NULL && file->real->pMethods != NULL) {
    close_rc = file->real->pMethods->xClose(file->real);
    if (close_rc == SQLITE_OK) {
      base_closed = 1;
      file->real->pMethods = NULL;
      if (qp_take_test_cleanup_fault("base_close")) {
        close_rc = SQLITE_IOERR;
        qp_record_base_close_error(file->artifact, "partial_open_close",
                                   close_rc);
      }
    } else {
      qp_record_base_close_error(file->artifact, "partial_open_close",
                                 close_rc);
    }
  }
  if (base_closed) {
    shm_proof_rc = qp_clear_shm_proof(file);
    source_proof_rc = qp_close_source_proof(file);
  }
  if (file->wal_placeholder || file->placeholder_path != NULL)
    delete_rc = qp_delete_placeholder(file);
  if (file->path != NULL) {
    qp_api->free(file->path);
    file->path = NULL;
  }
  if (file->counted_ref) QP_AUDIT_INC(partial_open_cleanup);
  qp_release_file_reference(file);
  if (close_rc != SQLITE_OK) return close_rc;
  if (shm_proof_rc != SQLITE_OK) return shm_proof_rc;
  if (source_proof_rc != SQLITE_OK) return source_proof_rc;
  if (delete_rc != SQLITE_OK) return delete_rc;
  return rc;
}

static int qp_vfs_open(sqlite3_vfs *vfs, sqlite3_filename name,
                       sqlite3_file *sqlite_file, int flags, int *out_flags) {
  QpFile *file = (QpFile *)sqlite_file;
  const char *token = NULL;
  const char *temp_path = NULL;
  const char *race_artifact = NULL;
  char *opened_path = NULL;
  QpIdentity expected_identity;
  QpIdentity path_identity;
  int safe_flags;
  int local_out_flags = 0;
  int rc;
  int unsafe;
  (void)vfs;
  memset(file, 0, sizeof(*file));
  file->real = (sqlite3_file *)((unsigned char *)sqlite_file +
                                QP_REAL_FILE_OFFSET);
#ifdef _WIN32
  file->proof_handle = INVALID_HANDLE_VALUE;
  file->shm_anchor = INVALID_HANDLE_VALUE;
#else
  file->proof_handle = -1;
  file->shm_anchor = -1;
#endif
  if (out_flags != NULL) *out_flags = 0;

  if (name == NULL) {
    file->kind = QP_FILE_TEMP;
    file->artifact = QP_ARTIFACT_TEMP;
    rc = qp_reserve_temp(file, &opened_path);
    if (rc != SQLITE_OK) return rc;
    safe_flags = flags;
    safe_flags &= ~(SQLITE_OPEN_READONLY | SQLITE_OPEN_URI);
    safe_flags |= SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE |
                  SQLITE_OPEN_EXCLUSIVE | SQLITE_OPEN_DELETEONCLOSE;
  } else {
    file->kind = QP_FILE_SOURCE;
    token = qp_api->uri_parameter(name, "qplot_token");
    if (flags & SQLITE_OPEN_MAIN_DB) {
      file->artifact = QP_ARTIFACT_MAIN;
      temp_path = qp_api->uri_parameter(name, "qplot_temp");
      race_artifact = qp_api->uri_parameter(name, "qplot_test_race");
      if (temp_path == NULL) {
        return SQLITE_CANTOPEN;
      }
      rc = qp_reserve_main(name, token, temp_path, race_artifact, file);
    } else if (flags & SQLITE_OPEN_WAL) {
      file->artifact = QP_ARTIFACT_WAL;
      rc = qp_reserve_derived(name, token, flags, file);
    } else if (flags & SQLITE_OPEN_MAIN_JOURNAL) {
      file->artifact = QP_ARTIFACT_JOURNAL;
      rc = qp_reserve_derived(name, token, flags, file);
      if (rc == SQLITE_OK) {
        /* Trusted-live accepts only a sidecar-free rollback-journal source.
         * Never delegate a journal open: even a read-only open would let the
         * SQLite pager enter hot-journal recovery before this wrapper blocks
         * the first write. */
        qp_record_failure(QP_FAILURE_UNSUPPORTED, QP_ARTIFACT_JOURNAL,
                          "journal_open", SQLITE_CANTOPEN);
        qp_release_file_reference(file);
        return SQLITE_CANTOPEN;
      }
    } else {
      return SQLITE_CANTOPEN;
    }
    if (rc != SQLITE_OK) return rc;
    opened_path = qp_strdup(name);
    if (opened_path == NULL) {
      qp_release_file_reference(file);
      return SQLITE_NOMEM;
    }
    file->path = opened_path;
    rc = qp_open_source_proof(file);
    if (rc != SQLITE_OK) {
      if (file->artifact == QP_ARTIFACT_WAL) {
        int path_state = qp_source_path_state(file);
        if (path_state == 0 &&
            !qp_expected_sidecar_requires_present(QP_ARTIFACT_WAL)) {
          rc = qp_create_wal_placeholder(file);
          if (rc != SQLITE_OK) {
            qp_record_failure(QP_FAILURE_IO, file->artifact,
                              "wal_placeholder", rc);
            return qp_partial_open_cleanup(file, rc);
          }
        } else {
          int failure_kind =
              qp_failure_kind_for_path_state(path_state, QP_FAILURE_IO);
          qp_record_failure(failure_kind, file->artifact, "proof_open", rc);
          return qp_partial_open_cleanup(file, rc);
        }
      } else {
        int path_state = qp_source_path_state(file);
        int failure_kind;
        if (path_state < 0) {
          failure_kind =
              qp_failure_kind_for_path_state(path_state, QP_FAILURE_IO);
        } else if (path_state > 0) {
          failure_kind = QP_FAILURE_IO;
        } else {
          failure_kind = file->artifact == QP_ARTIFACT_MAIN
                             ? QP_FAILURE_SOURCE_CHANGED
                             : QP_FAILURE_POLICY;
        }
        qp_record_failure(failure_kind, file->artifact, "proof_open", rc);
        return qp_partial_open_cleanup(file, rc);
      }
    }
    if (file->artifact == QP_ARTIFACT_WAL && !file->wal_placeholder &&
        !qp_accept_expected_sidecar_identity(QP_ARTIFACT_WAL,
                                             &file->proof_identity)) {
      return qp_partial_open_cleanup(file, SQLITE_CANTOPEN);
    }
    if (file->artifact == QP_ARTIFACT_WAL && !file->wal_placeholder) {
      int failure_kind = QP_FAILURE_IO;
      rc = qp_validate_wal_proof_header(file->proof_handle, &failure_kind);
      if (rc != SQLITE_OK) {
        qp_record_failure(failure_kind, QP_ARTIFACT_WAL, "wal_header", rc);
        return qp_partial_open_cleanup(file, rc);
      }
    }
    if (file->artifact == QP_ARTIFACT_MAIN) {
      if (!qp_parse_expected_identity(name, &expected_identity)) {
        qp_record_failure(QP_FAILURE_UNSUPPORTED, file->artifact,
                          "expected_identity_parse", SQLITE_CANTOPEN);
        return qp_partial_open_cleanup(file, SQLITE_CANTOPEN);
      }
      if (!qp_identities_equal(&expected_identity, &file->proof_identity)) {
        qp_record_failure(QP_FAILURE_SOURCE_CHANGED, file->artifact,
                          "expected_identity", SQLITE_CANTOPEN);
        return qp_partial_open_cleanup(file, SQLITE_CANTOPEN);
      }
    }
    rc = file->wal_placeholder
             ? SQLITE_OK
             : qp_test_race_barrier(file->artifact, "proof");
    if (rc != SQLITE_OK) return qp_partial_open_cleanup(file, rc);
    unsafe = flags & (SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE |
                      SQLITE_OPEN_DELETEONCLOSE | SQLITE_OPEN_EXCLUSIVE);
    if (unsafe) QP_AUDIT_INC(source_open_flags_stripped);
    safe_flags = flags & ~(SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE |
                           SQLITE_OPEN_DELETEONCLOSE | SQLITE_OPEN_EXCLUSIVE);
    safe_flags |= SQLITE_OPEN_READONLY | SQLITE_OPEN_NOFOLLOW;
    qp_lock();
    qp_state.audit.source_open_readonly++;
    if (safe_flags & SQLITE_OPEN_READWRITE)
      qp_state.audit.source_open_readwrite++;
    if (safe_flags & SQLITE_OPEN_CREATE) qp_state.audit.source_open_create++;
    if (safe_flags & SQLITE_OPEN_DELETEONCLOSE)
      qp_state.audit.source_open_delete_on_close++;
    qp_unlock();
  }

  if (file->path == NULL) file->path = opened_path;
  memset(file->real, 0, (size_t)qp_base_vfs->szOsFile);
  /* Preserve SQLite's hidden NUL-separated URI parameter tail.  The pinned
   * OS VFS receives no readonly_shm option: its exact SHM is intentionally
   * opened and mapped read/write while all xOpen source files remain RO. */
  rc = qp_base_vfs->xOpen(qp_base_vfs,
                          file->wal_placeholder
                              ? file->placeholder_path
                              : (file->kind == QP_FILE_SOURCE ? name
                                                              : opened_path),
                          file->real, safe_flags,
                          &local_out_flags);
  if (rc != SQLITE_OK || file->real->pMethods == NULL) {
    int result_rc = rc == SQLITE_OK ? SQLITE_CANTOPEN : rc;
    if (file->kind == QP_FILE_SOURCE) {
      int path_state;
      memset(&path_identity, 0, sizeof(path_identity));
      path_state = qp_current_path_identity(file->path, &path_identity,
                                            file->artifact);
      if (path_state != QP_PATH_IDENTITY_PRESENT ||
          !qp_identities_equal(&path_identity, &file->proof_identity)) {
        int sticky_rc = qp_file_sticky_failure(file);
        if (sticky_rc != SQLITE_OK) {
          result_rc = sticky_rc;
        } else if (path_state == QP_PATH_IDENTITY_UNSAFE) {
          result_rc = SQLITE_CANTOPEN;
          qp_record_failure(QP_FAILURE_UNSUPPORTED, file->artifact,
                            "base_open_path", result_rc);
        } else if (path_state == QP_PATH_IDENTITY_IO) {
          result_rc = SQLITE_IOERR;
          qp_record_failure(QP_FAILURE_IO, file->artifact,
                            "base_open_path", result_rc);
        } else {
          qp_record_failure(QP_FAILURE_SOURCE_CHANGED, file->artifact,
                            "base_open_path", result_rc);
        }
      } else {
        qp_record_failure(QP_FAILURE_IO, file->artifact, "base_open",
                          result_rc);
      }
    }
    return qp_partial_open_cleanup(file, result_rc);
  }
  if (file->kind == QP_FILE_SOURCE) {
    rc = file->wal_placeholder
             ? SQLITE_OK
             : qp_test_race_barrier(file->artifact, "actual");
    if (rc == SQLITE_OK) rc = qp_validate_source_binding(file, "open_actual");
    if (rc != SQLITE_OK) return qp_partial_open_cleanup(file, rc);
    if ((local_out_flags & SQLITE_OPEN_READWRITE) != 0 ||
        (local_out_flags & SQLITE_OPEN_READONLY) == 0) {
      if (local_out_flags & SQLITE_OPEN_READWRITE) {
        QP_AUDIT_INC(source_open_readwrite);
      }
      qp_record_failure(QP_FAILURE_POLICY, file->artifact, "open_mode",
                        SQLITE_READONLY);
      return qp_partial_open_cleanup(file, SQLITE_READONLY);
    }
    qp_active_file_add(file);
  }
  if (file->kind == QP_FILE_TEMP) QP_AUDIT_INC(temp_redirect);
  sqlite_file->pMethods = &qp_io_methods;
  if (out_flags != NULL) *out_flags = local_out_flags;
  return SQLITE_OK;
}

static int qp_vfs_delete(sqlite3_vfs *vfs, const char *name, int sync_dir) {
  int source;
  int temporary;
  int rc;
  (void)vfs;
  qp_lock();
  source = qp_is_source_family_locked(name);
  temporary = qp_is_temp_path_locked(name);
  if (source) qp_state.audit.source_delete++;
  if (temporary) qp_state.audit.temp_delete++;
  qp_unlock();
  if (source) return SQLITE_READONLY;
  if (!temporary) return SQLITE_CANTOPEN;
  rc = qp_base_vfs->xDelete(qp_base_vfs, name, sync_dir);
  if (rc != SQLITE_OK) {
    qp_lock();
    qp_state.cleanup_failed = 1;
    qp_set_failure_locked(QP_FAILURE_IO, QP_ARTIFACT_TEMP, "temp_delete",
                          rc);
    qp_unlock();
  }
  return rc;
}

static int qp_vfs_access(sqlite3_vfs *vfs, const char *name, int flags,
                         int *result) {
  int allowed;
  (void)vfs;
  qp_lock();
  allowed = qp_is_source_family_locked(name) || qp_is_temp_path_locked(name);
  qp_unlock();
  if (!allowed) {
    if (result != NULL) *result = 0;
    return SQLITE_OK;
  }
  return qp_base_vfs->xAccess(qp_base_vfs, name, flags, result);
}

static int qp_vfs_full_pathname(sqlite3_vfs *vfs, const char *name,
                                int capacity, char *result) {
  (void)vfs;
  return qp_base_vfs->xFullPathname(qp_base_vfs, name, capacity, result);
}

static void *qp_vfs_dl_open(sqlite3_vfs *vfs, const char *name) {
  (void)vfs; (void)name;
  return NULL;
}

static void qp_vfs_dl_error(sqlite3_vfs *vfs, int capacity, char *message) {
  (void)vfs;
  if (message != NULL && capacity > 0) {
    (void)snprintf(message, (size_t)capacity,
                   "extension loading is disabled by qPlot's trusted VFS");
  }
}

static void (*qp_vfs_dl_sym(sqlite3_vfs *vfs, void *handle,
                            const char *name))(void) {
  (void)vfs; (void)handle; (void)name;
  return NULL;
}

static void qp_vfs_dl_close(sqlite3_vfs *vfs, void *handle) {
  (void)vfs; (void)handle;
}

static int qp_vfs_randomness(sqlite3_vfs *vfs, int amount, char *output) {
  (void)vfs;
  return qp_base_vfs->xRandomness(qp_base_vfs, amount, output);
}

static int qp_vfs_sleep(sqlite3_vfs *vfs, int microseconds) {
  (void)vfs;
  return qp_base_vfs->xSleep(qp_base_vfs, microseconds);
}

static int qp_vfs_current_time(sqlite3_vfs *vfs, double *time_value) {
  (void)vfs;
  return qp_base_vfs->xCurrentTime(qp_base_vfs, time_value);
}

static int qp_vfs_last_error(sqlite3_vfs *vfs, int capacity, char *message) {
  (void)vfs;
  if (qp_base_vfs->xGetLastError == NULL) return 0;
  return qp_base_vfs->xGetLastError(qp_base_vfs, capacity, message);
}

static int qp_vfs_current_time_int64(sqlite3_vfs *vfs,
                                     sqlite3_int64 *time_value) {
  (void)vfs;
  if (qp_base_vfs->iVersion < 2 || qp_base_vfs->xCurrentTimeInt64 == NULL)
    return SQLITE_NOTFOUND;
  return qp_base_vfs->xCurrentTimeInt64(qp_base_vfs, time_value);
}

static int qp_vfs_set_system_call(sqlite3_vfs *vfs, const char *name,
                                  sqlite3_syscall_ptr call) {
  (void)vfs; (void)name; (void)call;
  return SQLITE_NOTFOUND;
}

static sqlite3_syscall_ptr qp_vfs_get_system_call(sqlite3_vfs *vfs,
                                                   const char *name) {
  (void)vfs; (void)name;
  return NULL;
}

static const char *qp_vfs_next_system_call(sqlite3_vfs *vfs,
                                            const char *name) {
  (void)vfs; (void)name;
  return NULL;
}

static void qp_audit_sql(sqlite3_context *context, int argc,
                         sqlite3_value **values) {
  const unsigned char *token;
  QpAudit audit;
  char json[QP_JSON_CAP];
  int length;
  if (argc != 1 || values == NULL ||
      (token = qp_api->value_text(values[0])) == NULL) {
    qp_api->result_error(context, "qplot audit requires a token", -1);
    return;
  }
  qp_lock();
  if (!qp_state.configured || strcmp((const char *)token, qp_state.token) != 0) {
    qp_unlock();
    qp_api->result_error(context, "unknown qplot trusted-VFS token", -1);
    return;
  }
  audit = qp_state.audit;
  qp_unlock();
  length = snprintf(
      json, sizeof(json),
      "{\"source_open_readonly\":%llu,\"source_open_readwrite\":%llu,"
      "\"source_open_create\":%llu,\"source_open_delete_on_close\":%llu,"
      "\"source_open_flags_stripped\":%llu,\"source_read\":%llu,"
      "\"source_read_bytes\":%llu,\"source_write\":%llu,"
      "\"source_truncate\":%llu,\"source_sync\":%llu,"
      "\"source_delete\":%llu,\"source_fetch\":%llu,"
      "\"source_writable_map\":%llu,\"shm_map_readonly\":%llu,"
      "\"shm_map_writable\":%llu,\"shm_map_extend\":%llu,"
      "\"shm_map_rejected\":%llu,\"shm_lock\":%llu,"
      "\"shm_unmap_delete_requested\":%llu,\"temp_redirect\":%llu,"
      "\"temp_write\":%llu,\"temp_write_bytes\":%llu,"
      "\"temp_delete\":%llu,\"stale_callback_rejected\":%llu,"
      "\"identity_verified\":%llu,\"identity_rejected\":%llu,"
      "\"proof_open\":%llu,\"proof_close\":%llu,"
      "\"proof_close_error\":%llu,\"proof_active\":%llu,"
      "\"proof_peak\":%llu,\"shm_unmap\":%llu,"
      "\"shm_unmap_error\":%llu,\"shm_unmap_delete_forwarded\":%llu,"
      "\"partial_open_cleanup\":%llu,\"base_close_error\":%llu}",
      audit.source_open_readonly, audit.source_open_readwrite,
      audit.source_open_create, audit.source_open_delete_on_close,
      audit.source_open_flags_stripped, audit.source_read,
      audit.source_read_bytes, audit.source_write, audit.source_truncate,
      audit.source_sync, audit.source_delete, audit.source_fetch,
      audit.source_writable_map, audit.shm_map_readonly,
      audit.shm_map_writable, audit.shm_map_extend, audit.shm_map_rejected,
      audit.shm_lock, audit.shm_unmap_delete_requested, audit.temp_redirect,
      audit.temp_write, audit.temp_write_bytes, audit.temp_delete,
      audit.stale_callback_rejected, audit.identity_verified,
      audit.identity_rejected, audit.proof_open, audit.proof_close,
      audit.proof_close_error, audit.proof_active, audit.proof_peak,
      audit.shm_unmap, audit.shm_unmap_error,
      audit.shm_unmap_delete_forwarded, audit.partial_open_cleanup,
      audit.base_close_error);
  if (length < 0 || length >= (int)sizeof(json)) {
    qp_api->result_error_toobig(context);
    return;
  }
  qp_api->result_text(context, json, length, QPLOT_SQLITE_TRANSIENT);
}

static void qp_status_sql(sqlite3_context *context, int argc,
                          sqlite3_value **values) {
  const unsigned char *token;
  QpU64 sequence;
  int kind;
  int artifact;
  int sqlite_code;
  char operation[QP_FAILURE_TEXT_CAP];
  char json[512];
  int length;
  if (argc != 1 || values == NULL ||
      (token = qp_api->value_text(values[0])) == NULL) {
    qp_api->result_error(context, "qplot status requires a token", -1);
    return;
  }
  qp_lock();
  if (!qp_state.configured || strcmp((const char *)token, qp_state.token) != 0) {
    qp_unlock();
    qp_api->result_error(context, "unknown qplot trusted-VFS token", -1);
    return;
  }
  sequence = qp_state.failure_sequence;
  kind = qp_state.failure_kind;
  artifact = qp_state.failure_artifact;
  sqlite_code = qp_state.failure_sqlite_code;
  memcpy(operation, qp_state.failure_operation, sizeof(operation));
  operation[sizeof(operation) - 1] = '\0';
  qp_unlock();
  length = snprintf(
      json, sizeof(json),
      "{\"sequence\":%llu,\"kind\":\"%s\",\"artifact\":\"%s\","
      "\"operation\":\"%s\",\"sqlite_code\":%d}",
      sequence, qp_failure_kind_name(kind), qp_artifact_name(artifact),
      operation, sqlite_code);
  if (length < 0 || length >= (int)sizeof(json)) {
    qp_api->result_error_toobig(context);
    return;
  }
  qp_api->result_text(context, json, length, QPLOT_SQLITE_TRANSIENT);
}

static void qp_validate_sql(sqlite3_context *context, int argc,
                            sqlite3_value **values) {
  const unsigned char *token;
  QpFile *file;
  int rc = SQLITE_OK;
  int cleanup_failed;
  if (argc != 1 || values == NULL ||
      (token = qp_api->value_text(values[0])) == NULL) {
    qp_api->result_error(context, "qplot validation requires a token", -1);
    return;
  }
  /* Holding the list mutex pins every wrapper allocation until validation is
   * complete.  Close removes from this list before releasing the real file. */
  qp_api->mutex_enter(qp_file_list_mutex);
  qp_lock();
  if (!qp_state.configured || strcmp((const char *)token, qp_state.token) != 0) {
    qp_unlock();
    qp_api->mutex_leave(qp_file_list_mutex);
    qp_api->result_error(context, "unknown qplot trusted-VFS token", -1);
    return;
  }
  cleanup_failed = qp_state.cleanup_failed;
  qp_unlock();
  if (cleanup_failed) rc = SQLITE_IOERR;
  for (file = qp_active_file_list; rc == SQLITE_OK && file != NULL;
       file = file->next_active) {
    if (!qp_file_is_current(file)) {
      rc = SQLITE_IOERR;
      break;
    }
    if (file->wal_placeholder) {
      rc = qp_promote_wal_if_present(file);
      if (rc != SQLITE_OK) break;
    }
    rc = qp_validate_no_rollback_journal(file);
    if (rc != SQLITE_OK) break;
    rc = qp_validate_source_binding(file, "explicit_validate");
    if (rc != SQLITE_OK) break;
    if (file->shm_active) {
      rc = qp_validate_shm_binding(file, "explicit_validate_shm");
      if (rc != SQLITE_OK) break;
    }
  }
  qp_api->mutex_leave(qp_file_list_mutex);
  if (rc != SQLITE_OK) {
    qp_api->result_error(context,
                         "qplot native source-handle validation failed", -1);
    qp_api->result_error_code(context, rc);
    return;
  }
  qp_api->result_int(context, 1);
}

static void qp_release_sql(sqlite3_context *context, int argc,
                           sqlite3_value **values) {
  const unsigned char *token;
  if (argc != 1 || values == NULL ||
      (token = qp_api->value_text(values[0])) == NULL) {
    qp_api->result_error(context, "qplot release requires a token", -1);
    return;
  }
  qp_api->mutex_enter(qp_file_list_mutex);
  qp_lock();
  if (!qp_state.configured) {
    qp_unlock();
    qp_api->mutex_leave(qp_file_list_mutex);
    qp_api->result_int(context, 0);
    return;
  }
  if (strcmp((const char *)token, qp_state.token) != 0) {
    qp_unlock();
    qp_api->mutex_leave(qp_file_list_mutex);
    qp_api->result_error(context, "unknown qplot trusted-VFS token", -1);
    return;
  }
  if (qp_state.active_files != 0 || qp_state.audit.proof_active != 0 ||
      qp_active_file_list != NULL || qp_state.cleanup_failed) {
    qp_unlock();
    qp_api->mutex_leave(qp_file_list_mutex);
    qp_api->result_error(context, "qplot trusted-VFS files are still open", -1);
    return;
  }
  {
    QpU64 generation = qp_state.generation;
    memset(&qp_state, 0, sizeof(qp_state));
    qp_state.generation = generation;
  }
  qp_unlock();
  qp_api->mutex_leave(qp_file_list_mutex);
  qp_api->result_int(context, 1);
}

static int qp_init_error(const sqlite3_api_routines *api, char **error_message,
                         const char *message, int rc) {
  if (error_message != NULL) *error_message = api->mprintf("%s", message);
  return rc;
}

QP_EXPORT int sqlite3_qplot_trusted_vfs_init(
    sqlite3 *database, char **error_message,
    const sqlite3_api_routines *api) {
  sqlite3_vfs *base;
  sqlite3_vfs *existing;
  sqlite3_mutex *vfs_mutex;
  int first_registration = 0;
  int rc;
#ifdef _WIN32
  const char *base_name = "win32";
#else
  const char *base_name = "unix";
#endif
  if (api == NULL) return SQLITE_MISUSE;
  if (api->mprintf == NULL) return SQLITE_MISUSE;
  if (api->libversion_number == NULL ||
      api->libversion_number() != QPLOT_TRUSTED_SQLITE_VERSION_NUMBER ||
      api->sourceid == NULL || api->sourceid() == NULL ||
      strcmp(api->sourceid(), QP_SQLITE_SOURCE_ID) != 0 ||
      api->malloc == NULL || api->free == NULL ||
      api->mutex_alloc == NULL || api->mutex_enter == NULL ||
      api->mutex_leave == NULL || api->vfs_find == NULL ||
      api->vfs_register == NULL || api->vfs_unregister == NULL ||
      api->create_function == NULL || api->uri_parameter == NULL ||
      api->value_text == NULL || api->result_error == NULL ||
      api->result_error_code == NULL || api->result_error_toobig == NULL ||
      api->result_int == NULL || api->result_text == NULL ||
      api->compileoption_used == NULL || api->xthreadsafe == NULL) {
    return qp_init_error(api, error_message,
                         "qPlot trusted VFS requires SQLite 3.53.4",
                         SQLITE_MISUSE);
  }
  if (api->xthreadsafe() == 0 || api->compileoption_used("OMIT_WAL") != 0 ||
      api->compileoption_used("SHM_DIRECTORY") != 0) {
    return qp_init_error(api, error_message,
                         "qPlot trusted VFS requires threaded, colocated WAL",
                         SQLITE_MISUSE);
  }
#ifndef _WIN32
  if (geteuid() == 0) {
    return qp_init_error(api, error_message,
                         "qPlot trusted VFS refuses POSIX root processes",
                         SQLITE_MISUSE);
  }
#endif
  vfs_mutex = api->mutex_alloc(SQLITE_MUTEX_STATIC_VFS2);
  if (vfs_mutex == NULL) {
    return qp_init_error(api, error_message,
                         "qPlot trusted VFS requires SQLite mutexes",
                         SQLITE_MISUSE);
  }
  api->mutex_enter(vfs_mutex);
  if (qp_api != NULL && qp_api != api) {
    api->mutex_leave(vfs_mutex);
    return qp_init_error(api, error_message,
                         "qPlot trusted VFS is already bound to another SQLite",
                         SQLITE_MISUSE);
  }
  if (qp_api == NULL) qp_api = api;
#ifdef _WIN32
  if (!qp_windows_process_is_unprivileged()) {
    api->mutex_leave(vfs_mutex);
    return qp_init_error(api, error_message,
                         "qPlot trusted VFS refuses elevated Windows tokens",
                         SQLITE_MISUSE);
  }
  if (!qp_windows_resolve_nt_query_object()) {
    api->mutex_leave(vfs_mutex);
    return qp_init_error(
        api, error_message,
        "qPlot trusted VFS cannot verify Windows handle access rights",
        SQLITE_MISUSE);
  }
#endif
  if (qp_state_mutex == NULL) qp_state_mutex = api->mutex_alloc(SQLITE_MUTEX_FAST);
  if (qp_file_list_mutex == NULL) {
    qp_file_list_mutex = api->mutex_alloc(SQLITE_MUTEX_FAST);
  }
  if (qp_state_mutex == NULL || qp_file_list_mutex == NULL) {
    api->mutex_leave(vfs_mutex);
    return qp_init_error(api, error_message,
                         "qPlot trusted VFS could not allocate its mutex",
                         SQLITE_NOMEM);
  }
  base = api->vfs_find(base_name);
  if (base == NULL || base->iVersion < 3 || base->xOpen == NULL ||
      base->xDelete == NULL || base->xAccess == NULL ||
      base->xFullPathname == NULL || base->szOsFile <= 0 ||
      base->mxPathname <= 0 ||
#ifdef _WIN32
      base->szOsFile < (int)sizeof(QpPinnedWinFile) ||
#else
      base->szOsFile < (int)sizeof(QpPinnedUnixFile) ||
#endif
      QP_REAL_FILE_OFFSET > INT_MAX - base->szOsFile) {
    api->mutex_leave(vfs_mutex);
    return qp_init_error(api, error_message,
                         "qPlot trusted VFS cannot bind the pinned OS VFS",
                         SQLITE_CANTOPEN);
  }
  if (qp_registered && base != qp_base_vfs) {
    api->mutex_leave(vfs_mutex);
    return qp_init_error(api, error_message,
                         "qPlot trusted VFS base VFS changed after registration",
                         SQLITE_MISUSE);
  }
  existing = api->vfs_find(QP_VFS_NAME);
  if (existing != NULL && existing != &qp_vfs) {
    api->mutex_leave(vfs_mutex);
    return qp_init_error(api, error_message,
                         "the qPlot trusted VFS name is already occupied",
                         SQLITE_MISUSE);
  }
  if (!qp_registered) {
    qp_base_vfs = base;
    memset(&qp_vfs, 0, sizeof(qp_vfs));
    qp_vfs.iVersion = 3;
    qp_vfs.szOsFile = QP_REAL_FILE_OFFSET + qp_base_vfs->szOsFile;
    qp_vfs.mxPathname = qp_base_vfs->mxPathname;
    qp_vfs.zName = QP_VFS_NAME;
    qp_vfs.pAppData = qp_base_vfs;
    qp_vfs.xOpen = qp_vfs_open;
    qp_vfs.xDelete = qp_vfs_delete;
    qp_vfs.xAccess = qp_vfs_access;
    qp_vfs.xFullPathname = qp_vfs_full_pathname;
    qp_vfs.xDlOpen = qp_vfs_dl_open;
    qp_vfs.xDlError = qp_vfs_dl_error;
    qp_vfs.xDlSym = qp_vfs_dl_sym;
    qp_vfs.xDlClose = qp_vfs_dl_close;
    qp_vfs.xRandomness = qp_vfs_randomness;
    qp_vfs.xSleep = qp_vfs_sleep;
    qp_vfs.xCurrentTime = qp_vfs_current_time;
    qp_vfs.xGetLastError = qp_vfs_last_error;
    qp_vfs.xCurrentTimeInt64 = qp_vfs_current_time_int64;
    qp_vfs.xSetSystemCall = qp_vfs_set_system_call;
    qp_vfs.xGetSystemCall = qp_vfs_get_system_call;
    qp_vfs.xNextSystemCall = qp_vfs_next_system_call;
    rc = api->vfs_register(&qp_vfs, 0);
    if (rc != SQLITE_OK) {
      qp_base_vfs = NULL;
      api->mutex_leave(vfs_mutex);
      return qp_init_error(api, error_message,
                           "qPlot trusted VFS registration failed", rc);
    }
    qp_registered = 1;
    first_registration = 1;
  }
  rc = api->create_function(database, "qplot_trusted_vfs_audit", 1,
                            SQLITE_UTF8 | SQLITE_DIRECTONLY, NULL,
                            qp_audit_sql, NULL, NULL);
  if (rc == SQLITE_OK) {
    rc = api->create_function(database, "qplot_trusted_vfs_status", 1,
                              SQLITE_UTF8 | SQLITE_DIRECTONLY, NULL,
                              qp_status_sql, NULL, NULL);
  }
  if (rc == SQLITE_OK) {
    rc = api->create_function(database, "qplot_trusted_vfs_validate", 1,
                              SQLITE_UTF8 | SQLITE_DIRECTONLY, NULL,
                              qp_validate_sql, NULL, NULL);
  }
  if (rc == SQLITE_OK) {
    rc = api->create_function(database, "qplot_trusted_vfs_release", 1,
                              SQLITE_UTF8 | SQLITE_DIRECTONLY, NULL,
                              qp_release_sql, NULL, NULL);
  }
  if (rc != SQLITE_OK && first_registration) {
    api->vfs_unregister(&qp_vfs);
    qp_registered = 0;
    qp_base_vfs = NULL;
  }
  api->mutex_leave(vfs_mutex);
  if (rc != SQLITE_OK) {
    return qp_init_error(api, error_message,
                         "qPlot trusted VFS SQL bootstrap failed", rc);
  }
  return SQLITE_OK_LOAD_PERMANENTLY;
}

static PyModuleDef qp_python_module = {
  PyModuleDef_HEAD_INIT,
  "_trusted_vfs_native",
  "Native boundary for qPlot's pinned trusted SQLite reader.",
  -1,
  NULL,
  NULL,
  NULL,
  NULL,
  NULL
};

PyMODINIT_FUNC PyInit__trusted_vfs_native(void) {
  PyObject *module = PyModule_Create(&qp_python_module);
  if (module != NULL) {
    if (PyModule_AddStringConstant(module, "sqlite_version", "3.53.4") < 0 ||
        PyModule_AddStringConstant(module, "vfs_name", QP_VFS_NAME) < 0) {
      Py_DECREF(module);
      return NULL;
    }
  }
  return module;
}
