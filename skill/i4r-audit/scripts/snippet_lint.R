## snippet_lint.R <setup.R> <prelude.R|""> <snippets-dir|""> <C-n=path>...
##
## Static warning for one defect class only: a snippet (or the prelude) calls, unqualified, a function
## an installed package documents, while nothing the notebook runs before it attaches that package.
## The release notebook then records "could not find function" for the claim, although an operator
## harness that attached the package passed it (the Perfectionism audit, 2026-09-30: five claims).
## Called by make_verification_notebook.sh; prints "WARN: ..." lines on stdout and always exits 0.
##
## It stays quiet whenever it cannot be sure:
##   * calls inside non-standard evaluation: quote/bquote/substitute/expression/alist, formulas,
##     `[`/`[[` (data.table's .() and J()), and qualified calls into data-masking packages;
##   * a name that appears anywhere as a plain symbol or a formal (it may hold a function);
##   * a name no installed package documents (a typo or a dynamic definition: the render says which);
##   * everything after a construct that can define global functions invisibly: attach(), a
##     library() whose package is not a literal, source() of a file it cannot resolve into the
##     global environment, and assign/eval/evalq/load/list2env/sys.source without an explicit
##     non-global envir. source(file.path(SNIPS_DIR, "<file>")) is followed and linted as a helper.
args <- commandArgs(TRUE)
setup_file <- args[1]; prelude_file <- args[2]; snips_dir <- args[3]; claim_args <- args[-(1:3)]

NSE_FUNS <- c("quote", "bquote", "substitute", "expression", "alist", "~", "[", "[[", "Quote")
MASKING_PKGS <- c("dplyr", "tidyr", "data.table", "dtplyr", "ggplot2", "plyr", "rlang")
GLOBAL_ENVS <- c("globalenv()", ".GlobalEnv", "environment()", "parent.frame()", "TRUE", "T")

parsed <- function(path) {
  p <- tryCatch(parse(file = path, keep.source = TRUE), error = function(e) NULL)
  if (is.null(p)) return(NULL)
  d <- utils::getParseData(p)
  if (is.null(d)) d <- data.frame(token = character(), text = character(), line1 = integer())
  list(exprs = p, pd = d)
}
is_empty <- function(x) identical(x, quote(expr = ))
dep <- function(x) paste(deparse(x, width.cutoff = 500L), collapse = " ")

## the envir-like argument of a call, by name or position; NULL when absent
arg_of <- function(e, name, pos) {
  a <- as.list(e)[-1]; nm <- names(a); if (is.null(nm)) nm <- rep("", length(a))
  if (name %in% nm) return(a[[which(nm == name)[1]]])
  unnamed <- which(nm == "")
  if (!is.na(pos) && length(unnamed) >= pos) return(a[[unnamed[pos]]])
  NULL
}
targets_global <- function(env) is.null(env) || dep(env) %in% GLOBAL_ENVS

## source() target we can follow: "lit", file.path(SNIPS_DIR, "lit"), file.path(AUDIT_DIR, ..., "lit")
resolve_source <- function(x) {
  lit <- NULL
  if (is.character(x) && length(x) == 1) lit <- x
  if (is.call(x) && identical(x[[1]], as.name("file.path")) && length(x) >= 3 &&
      all(vapply(as.list(x)[-(1:2)], is.character, logical(1))) &&
      dep(x[[2]]) %in% c("SNIPS_DIR", "AUDIT_DIR"))
    lit <- do.call(file.path, as.list(as.list(x)[-(1:2)]))
  if (is.null(lit) || !nzchar(snips_dir)) return(NULL)
  for (cand in c(file.path(snips_dir, lit), file.path(dirname(snips_dir), lit), lit))
    if (file.exists(cand)) return(normalizePath(cand))
  NULL
}

## One pass over a file's AST: unqualified calls outside NSE, attached packages, followed sources,
## and whether it holds a construct that can define global functions invisibly.
scan <- function(exprs) {
  out <- new.env(); out$calls <- character(); out$pkgs <- character(); out$sourced <- character()
  out$dynamic <- character()
  walk <- function(e, nse) {
    if (!is.call(e)) {
      if (is.expression(e) || is.list(e) || is.pairlist(e)) {
        items <- as.list(e)
        for (i in seq_along(items)) if (!is_empty(items[[i]])) walk(items[[i]], nse)
      }
      return(invisible())
    }
    f <- e[[1]]; qualified <- is.call(f) && dep(f[[1]]) %in% c("::", ":::")
    fname <- if (is.symbol(f)) as.character(f) else if (qualified) as.character(f[[3]]) else ""
    child_nse <- nse || (fname %in% NSE_FUNS && (is.symbol(f) || qualified)) ||
                 (qualified && as.character(f[[2]]) %in% MASKING_PKGS)
    if (!nse && is.symbol(f) && !fname %in% c("function", "(", "{")) out$calls <- c(out$calls, fname)
    if (!nse && fname %in% c("library", "require")) {
      co <- arg_of(e, "character.only", NA); pkg <- arg_of(e, "package", 1)
      if (!is.null(co) && !identical(co, FALSE)) out$dynamic <- c(out$dynamic, "library(character.only)")
      else if (is.symbol(pkg) || is.character(pkg)) out$pkgs <- c(out$pkgs, as.character(pkg))
      else out$dynamic <- c(out$dynamic, "library(<expression>)")
    }
    if (!nse && fname == "source") {
      target <- resolve_source(arg_of(e, "file", 1)); local <- arg_of(e, "local", 2)
      if (!is.null(target)) out$sourced <- c(out$sourced, target)
      else if (targets_global(local) || identical(local, FALSE)) out$dynamic <- c(out$dynamic, "source()")
    }
    if (!nse && fname == "attach") out$dynamic <- c(out$dynamic, "attach()")
    if (!nse && fname %in% c("assign", "delayedAssign", "makeActiveBinding", "list2env") &&
        targets_global(arg_of(e, if (fname == "delayedAssign") "assign.env" else "envir", NA)))
      out$dynamic <- c(out$dynamic, paste0(fname, "()"))
    if (!nse && fname %in% c("eval", "evalq", "load", "sys.source") &&
        targets_global(arg_of(e, "envir", 2)))
      out$dynamic <- c(out$dynamic, paste0(fname, "()"))
    items <- as.list(e)
    start <- if (is.symbol(f) || qualified) 2 else 1
    if (start == 1 && is.call(f) && dep(f[[1]]) %in% c("$", "@")) { walk(f[[2]], nse); start <- 2 }
    for (i in seq_along(items)) if (i >= start && !is_empty(items[[i]])) walk(items[[i]], child_nse)
  }
  for (e in exprs) walk(e, FALSE)
  out
}

sources <- list()
add <- function(label, path) {
  if (!nzchar(path) || !file.exists(path)) return(invisible())
  sources[[length(sources) + 1]] <<- list(label = label, path = path, p = parsed(path))
}
add("setup chunk", setup_file)
add("_prelude.R", prelude_file)
for (ca in claim_args) {
  cut <- regexpr("=", ca, fixed = TRUE)
  add(substr(ca, 1, cut - 1), substr(ca, cut + 1, nchar(ca)))
}
## helpers the snippets source by a path we can resolve are linted like the prelude, first
helpers <- unique(unlist(lapply(sources, function(s) if (is.null(s$p)) character() else scan(s$p$exprs)$sourced)))
helpers <- setdiff(helpers, vapply(sources, function(s) normalizePath(s$path), ""))
if (length(helpers)) {
  head_n <- sum(vapply(sources, function(s) s$label %in% c("setup chunk", "_prelude.R"), logical(1)))
  sources <- c(sources[seq_len(head_n)],
               lapply(helpers, function(h) list(label = basename(h), path = h, p = parsed(h))),
               sources[setdiff(seq_along(sources), seq_len(head_n))])
}

maybe_defined <- unique(unlist(lapply(sources, function(s)
  if (is.null(s$p)) character() else s$p$pd$text[s$p$pd$token %in% c("SYMBOL", "SYMBOL_FORMALS")])))

alias_index <- local({
  al <- character(); pk <- character()
  for (lib in .libPaths()) for (pkg in list.files(lib)) {
    f <- file.path(lib, pkg, "help", "AnIndex")
    if (!file.exists(f)) next
    a <- tryCatch(sub("\t.*$", "", readLines(f, warn = FALSE)), error = function(e) character())
    al <- c(al, a); pk <- c(pk, rep(pkg, length(a)))
  }
  lapply(split(pk, al), unique)
})
installed <- rownames(utils::installed.packages())
attach_quietly <- function(pkg) isTRUE(tryCatch(suppressWarnings(suppressPackageStartupMessages(
  require(pkg, character.only = TRUE, quietly = TRUE))), error = function(e) FALSE))
first_lines <- function(pd, nm) sort(unique(pd$line1[pd$token == "SYMBOL_FUNCTION_CALL" & pd$text == nm]))

stopped <- ""
for (s in sources) {
  if (is.null(s$p)) next                          # a parse error: the render reports it
  sc <- scan(s$p$exprs)
  for (pkg in unique(sc$pkgs)) {
    if (!pkg %in% installed)
      cat(sprintf("WARN: %s: library(%s) will fail in the notebook: %s is not installed in this R (%s)\n",
                  s$label, pkg, pkg, R.version.string))
    else attach_quietly(pkg)
  }
  if (s$label == "setup chunk") next              # the reference; its load() fills a private env
  if (nzchar(stopped)) next
  names <- unique(sc$calls)
  names <- names[!names %in% maybe_defined]
  names <- names[!vapply(names, exists, logical(1), envir = globalenv(), mode = "function")]
  for (nm in names) {
    owners <- setdiff(alias_index[[nm]], .packages())
    if (!length(owners)) next
    cat(sprintf("WARN: %s calls %s() (line %s), which the setup chunk, _prelude.R and the snippets before it never attach (documented by: %s); the notebook will record \"could not find function\". Attach it in verification_snippets/_prelude.R, or write %s::%s().\n",
                s$label, nm, paste(head(first_lines(s$p$pd, nm), 3), collapse = ", "),
                paste(head(owners, 4), collapse = ", "), owners[1], nm))
  }
  if (length(sc$dynamic)) stopped <- sprintf("%s (%s)", s$label, paste(unique(sc$dynamic), collapse = ", "))
}
if (nzchar(stopped)) cat(sprintf("NOTE: snippet lint checked nothing after %s: it can define functions the lint cannot see\n", stopped))
