"""``sort_table_columns`` ignores column order inside tables and nothing else."""

from opspilot.schema import sort_table_columns

FRESH = """CREATE TABLE public.t (
    a uuid NOT NULL,
    b text DEFAULT 'open'::text NOT NULL,
    c integer DEFAULT 0 NOT NULL,
    CONSTRAINT t_c_check CHECK ((c >= 0))
);
CREATE INDEX t_a_idx ON public.t USING btree (a);
"""

GROWN = """CREATE TABLE public.t (
    a uuid NOT NULL,
    c integer DEFAULT 0 NOT NULL,
    b text DEFAULT 'open'::text NOT NULL,
    CONSTRAINT t_c_check CHECK ((c >= 0))
);
CREATE INDEX t_a_idx ON public.t USING btree (a);
"""


def test_reordered_columns_compare_equal() -> None:
    assert FRESH != GROWN
    assert sort_table_columns(FRESH) == sort_table_columns(GROWN)


def test_changed_type_or_default_still_differs() -> None:
    typed = GROWN.replace("c integer DEFAULT 0", "c bigint DEFAULT 0")
    defaulted = GROWN.replace(
        "b text DEFAULT 'open'::text", "b text DEFAULT 'new'::text"
    )
    assert sort_table_columns(FRESH) != sort_table_columns(typed)
    assert sort_table_columns(FRESH) != sort_table_columns(defaulted)


def test_column_on_one_side_only_differs() -> None:
    extra = GROWN.replace("    b text", "    z text,\n    b text")
    missing = GROWN.replace("    c integer DEFAULT 0 NOT NULL,\n", "")
    assert sort_table_columns(FRESH) != sort_table_columns(extra)
    assert sort_table_columns(FRESH) != sort_table_columns(missing)


def test_constraints_stay_after_the_columns_and_outside_is_untouched() -> None:
    constraint_first = GROWN.replace(
        "    a uuid NOT NULL,\n",
        "    CONSTRAINT t_c_check CHECK ((c >= 0)),\n    a uuid NOT NULL,\n",
    ).replace("    CONSTRAINT t_c_check CHECK ((c >= 0))\n);", "    b2 text\n);")
    lines = sort_table_columns(constraint_first).splitlines()
    assert lines[-3] == "    CONSTRAINT t_c_check CHECK ((c >= 0))"
    assert lines[-2:] == [");", "CREATE INDEX t_a_idx ON public.t USING btree (a);"]
    # A different constraint is not column order.
    assert sort_table_columns(FRESH) != sort_table_columns(
        GROWN.replace("(c >= 0)", "(c > 0)")
    )
    assert sort_table_columns(FRESH) != sort_table_columns(
        GROWN.replace("USING btree (a)", "USING btree (b)")
    )


def test_trailing_commas_are_renormalized() -> None:
    sorted_fresh = sort_table_columns(FRESH)
    block = sorted_fresh.split("CREATE TABLE public.t (\n", 1)[1].split("\n);", 1)[0]
    entries = block.split("\n")
    assert all(entry.endswith(",") for entry in entries[:-1])
    assert not entries[-1].endswith(",")
    assert sorted_fresh == sort_table_columns(sorted_fresh)
