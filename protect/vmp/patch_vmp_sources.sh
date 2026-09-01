#!/usr/bin/env bash
# Apply compatibility patches to a VMProtect 3.5.1 source tree so it builds
# with modern Linux toolchains (clang >= 15, glibc >= 2.32).
#
# Usage: patch_vmp_sources.sh <vmp-source-root>
set -euo pipefail

ROOT="${1:?usage: patch_vmp_sources.sh <vmp-source-root>}"

# 1) sys/sysctl.h was removed from glibc 2.32+; the calls are macOS-only, so a
#    stub header is enough to satisfy the unconditional includes.
mkdir -p "$ROOT/include/sys"
if [ ! -f "$ROOT/include/sys/sysctl.h" ]; then
    cat > "$ROOT/include/sys/sysctl.h" <<'EOF'
#ifndef _COMPAT_SYS_SYSCTL_H
#define _COMPAT_SYS_SYSCTL_H

#include <sys/types.h>
#include <errno.h>
#include <unistd.h>

static inline int sysctl(const int *name, unsigned int namelen, void *oldp, size_t *oldlenp, const void *newp, size_t newlen)
{
    (void)name; (void)namelen; (void)oldp; (void)oldlenp; (void)newp; (void)newlen;
    errno = ENOSYS;
    return -1;
}

#endif
EOF
    echo "created $ROOT/include/sys/sysctl.h"
fi

# 2) clang defines __rdtsc as a builtin; rename the inline implementation and
#    all call sites so the definition no longer collides.
for file in crypto.h core.cc loader.cc hwid.cc crypto.cc; do
    target="$ROOT/runtime/$file"
    if [ -f "$target" ]; then
        sed -i 's/__rdtsc/vmp_rdtsc/g' "$target"
    fi
done
echo "renamed __rdtsc -> vmp_rdtsc in runtime/"

# 3) Replace Windows-only debug output in Core::Compile().
python3 - "$ROOT/core/core.cc" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
old = 'OutputDebugStringA(string_format("rand_seed:%d\\n", rand_seed).c_str());'
new = 'std::cout << "rand_seed: " << rand_seed << std::endl;'
if old in text:
    text = text.replace(old, new)
    print("patched OutputDebugStringA in core.cc")
else:
    print("OutputDebugStringA pattern not found (already patched?)")

# OpenSSL 3 made RSA opaque; use accessor functions instead of struct members.
replacements = [
    ("OpenSSL::BN_bn2bin(r->n, data);", "OpenSSL::BN_bn2bin(OpenSSL::RSA_get0_n(r), data);"),
    ("OpenSSL::BN_bn2bin(r->e, data);", "OpenSSL::BN_bn2bin(OpenSSL::RSA_get0_e(r), data);"),
    ("OpenSSL::BN_bn2bin(r->d, data);", "OpenSSL::BN_bn2bin(OpenSSL::RSA_get0_d(r), data);"),
    ("BN_num_bytes(r->n)", "BN_num_bytes(OpenSSL::RSA_get0_n(r))"),
    ("BN_num_bytes(r->e)", "BN_num_bytes(OpenSSL::RSA_get0_e(r))"),
    ("BN_num_bytes(r->d)", "BN_num_bytes(OpenSSL::RSA_get0_d(r))"),
    ("delete data;", "delete[] data;"),
]
changed = 0
for old, new in replacements:
    if old in text:
        text = text.replace(old, new)
        changed += 1
path.write_text(text, encoding="utf-8")
print(f"patched {changed} OpenSSL RSA/new-delete patterns in core.cc")
PY

# 5) <intrin.h> is a MSVC/clang-ism; on Linux use <x86intrin.h> instead.
for file in "$ROOT/core/processors.cc" "$ROOT/core/intel.cc" "$ROOT/runtime/precompiled.h"; do
    if [ -f "$file" ]; then
        sed -i 's/#include <intrin.h>/#include <x86intrin.h>/g' "$file"
    fi
done
echo "replaced <intrin.h> with <x86intrin.h>"

# 6) clang requires <stdarg.h> to be included explicitly for va_start/va_end.
for header in "$ROOT/core/precompiled.h" "$ROOT/runtime/precompiled.h"; do
    if [ -f "$header" ] && ! grep -q '#include <stdarg.h>' "$header"; then
        sed -i '1i #include <stdarg.h>' "$header"
        echo "added <stdarg.h> to $header"
    fi
done

# 7) precommon.h defines its own strlcpy; modern glibc (>= 2.38) already
#    declares one in <string.h>, which conflicts. Nothing else in the tree
#    calls it, so drop the private definition entirely.
python3 - "$ROOT/runtime/precommon.h" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
for keyword in ("static inline size_t strlcpy", "inline size_t strlcpy"):
    start = text.find(keyword)
    if start != -1:
        line_start = text.rfind("\n", 0, start) + 1
        end = text.find("}\n", start)
        if end != -1:
            text = text[:line_start] + text[end + 2:]
            print("removed private strlcpy definition from precommon.h")
            break
path.write_text(text, encoding="utf-8")
PY

# 8) The leaked tree has mismatched method signatures between intel.h and
#    intel.cc; align the definitions with the declarations.
python3 - "$ROOT/core/intel.cc" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
replacements = [
    (
        "void IntelFunction::Mutate(const CompileContext &ctx, bool for_virtualization)",
        "void IntelFunction::Mutate(const CompileContext &ctx, bool for_virtualization, int index)",
    ),
    (
        "void IntelObfuscation::Compile(IntelFunction *func, size_t index)",
        "void IntelObfuscation::Compile(IntelFunction *func, size_t index, size_t end_index, bool for_virtualization)",
    ),
]
changed = 0
for old, new in replacements:
    if old in text:
        text = text.replace(old, new)
        changed += 1
path.write_text(text, encoding="utf-8")
print(f"patched {changed} Intel signatures in intel.cc")
PY

# 9) core/version.h is normally generated by version.bat on Windows.
VERSION_H="$ROOT/core/version.h"
if [ ! -f "$VERSION_H" ]; then
    printf '#define VER_MAJOR 3\n#define VER_MINOR 5\n#define VER_PATCH 1\n#define VER_BUILD 0\n#define VER_FILE "3.5.1.0"\n#define VER_PRODUCT "3.5.1"\n' > "$VERSION_H"
    echo "generated $VERSION_H"
fi

# 10) Linux ELF output fixes (leak's ELF rewriter is incomplete).
#     a) PT_GNU_PROPERTY must be dropped like PT_NOTE: the rewriter overwrites
#        the note's file offset with the packed payload but leaves the stale
#        program header, so glibc's ld.so crashes while parsing the note.
#        Without this fix every packed ELF dies in _dl_process_pt_gnu_property.
#     b) The rewriter empties the original PT_LOAD segments (p_filesz=0 and
#        p_memsz=0). The embedded runtime unpacks the original sections into
#        those virtual addresses and needs the pages mapped, so the mappings
#        are restored at the final phdr write. This gets past the runtime's
#        mprotect step, but is NOT sufficient: with cpPack the packer collects
#        segments (already emptied) instead of sections, so no code is packed
#        and the restored pages stay zero-filled. That remaining bug is not
#        fixed by this patch.
python3 - "$ROOT/core/elf.h" "$ROOT/core/elffile.cc" <<'PY'
import pathlib
import sys

elf_h = pathlib.Path(sys.argv[1])
text = elf_h.read_text(encoding="utf-8")
old = "\tPT_GNU_EH_FRAME  = 0x6474e550,"
if old in text and "PT_GNU_PROPERTY" not in text:
    text = text.replace(old, old + "\n\tPT_GNU_PROPERTY = 0x6474e553,")
    print("elf.h: added PT_GNU_PROPERTY define")
elf_h.write_text(text, encoding="utf-8")

cc = pathlib.Path(sys.argv[2])
text = cc.read_text(encoding="utf-8")

count_old = """\tif (ctx.options.flags & cpStripDebugInfo) {
\t\tfor (i = 0; i < segment_list_->count(); i++) {
\t\t\tsegment = segment_list_->item(i);
\t\t\tif (segment->type() == PT_NOTE)
\t\t\t\tnew_segment_count--;
\t\t}
\t}
"""
count_new = count_old + """\tfor (i = 0; i < segment_list_->count(); i++) {
\t\tsegment = segment_list_->item(i);
\t\tif (segment->type() == PT_GNU_PROPERTY)
\t\t\tnew_segment_count--;
\t}
"""
if count_old in text and "segment->type() == PT_GNU_PROPERTY" not in text:
    text = text.replace(count_old, count_new)
    print("elffile.cc: count GNU_PROPERTY in new_segment_count")

del_old = """\t\tfor (i = segment_list_->count(); i > 0; i--) {
\t\t\tsegment = segment_list_->item(i - 1);
\t\t\tif (segment->type() == PT_NOTE)
\t\t\t\tdelete segment;
\t\t}
\t}

\t// resize header
"""
del_new = """\t\tfor (i = segment_list_->count(); i > 0; i--) {
\t\t\tsegment = segment_list_->item(i - 1);
\t\t\tif (segment->type() == PT_NOTE)
\t\t\t\tdelete segment;
\t\t}
\t}

\t// The ELF rewriter cannot preserve GNU property notes: their original file
\t// offset is overwritten by the packed payload, so glibc's loader crashes
\t// while parsing the stale PT_GNU_PROPERTY. Drop the segment unconditionally.
\tfor (i = segment_list_->count(); i > 0; i--) {
\t\tsegment = segment_list_->item(i - 1);
\t\tif (segment->type() == PT_GNU_PROPERTY)
\t\t\tdelete segment;
\t}

\t// resize header
"""
if del_old in text and "while parsing the stale PT_GNU_PROPERTY" not in text:
    text = text.replace(del_old, del_new)
    print("elffile.cc: delete GNU_PROPERTY segment")

cc.write_text(text, encoding="utf-8")
PY

echo "patch_vmp_sources.sh: done"
