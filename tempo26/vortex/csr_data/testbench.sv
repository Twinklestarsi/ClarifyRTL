`timescale 1ns/1ps
`default_nettype none

module VX_csr_data_tb;
    localparam integer XLEN = 32;

    logic clk = 1'b0;
    logic reset = 1'b1;
    logic [63:0] cycles = 64'h1234_5678_9abc_def0;
    logic [63:0] instret = 64'h0123_4567_89ab_cdef;
    logic [3:0] active_warps = 4'b1011;
    logic [7:0] thread_mask = 8'b1010_0101;
    logic read_enable = 1'b0;
    logic [11:0] read_addr = '0;
    logic [1:0] read_wid = '0;
    wire read_valid;
    wire [31:0] read_data;
    wire read_error;
    logic write_enable = 1'b0;
    logic [11:0] write_addr = '0;
    logic [31:0] write_data = '0;
    wire write_ready;
    wire read_valid64;
    wire [63:0] read_data64;
    wire read_error64;
    wire write_ready64;

    integer errors = 0;
    integer checks = 0;
    integer shadow_reads = 0;
    integer shadow_writes = 0;
    integer shadow_errors = 0;
    logic [10:0] coverage = '0;

    always #5 clk = ~clk;

    VX_csr_data #(
        .XLEN(XLEN),
        .NUM_WARPS(4),
        .NUM_THREADS(8),
        .CORE_ID(7),
        .STARTUP_ARG(32'h1020_3040),
        .VENDOR_ID(32'h0000_002a),
        .ARCH_ID(32'h0000_003b),
        .IMPL_ID(32'h0000_004c),
        .NUM_CORES(2),
        .NUM_CLUSTERS(3),
        .LMEM_BASE(32'h8000_0000)
    ) dut (.*);

    VX_csr_data #(
        .XLEN(64),
        .NUM_WARPS(4),
        .NUM_THREADS(8),
        .CORE_ID(7),
        .STARTUP_ARG(64'h1020_3040),
        .VENDOR_ID(64'h2a),
        .ARCH_ID(64'h3b),
        .IMPL_ID(64'h4c),
        .NUM_CORES(2),
        .NUM_CLUSTERS(3),
        .LMEM_BASE(64'h8000_0000)
    ) dut64 (
        .clk(clk), .reset(reset), .cycles(cycles), .instret(instret),
        .active_warps(active_warps), .thread_mask(thread_mask),
        .read_enable(read_enable), .read_addr(read_addr), .read_wid(read_wid),
        .read_valid(read_valid64), .read_data(read_data64),
        .read_error(read_error64), .write_enable(1'b0), .write_addr('0),
        .write_data('0), .write_ready(write_ready64)
    );

    task automatic check(input logic condition, input string message);
        begin
            checks = checks + 1;
            if (!condition) begin
                errors = errors + 1;
                $display("ERROR %0t: %s", $time, message);
            end
        end
    endtask

    task automatic expect_read64(
        input logic [11:0] address,
        input logic [63:0] expected
    );
        begin
            @(negedge clk);
            read_enable = 1'b1;
            read_addr = address;
            read_wid = 2'd0;
            @(posedge clk);
            #1;
            check(read_valid64 && !read_error64,
                  "XLEN=64 read valid/error mismatch");
            check(read_data64 === expected, $sformatf(
                "XLEN=64 CSR %03h got=%016h expected=%016h",
                address, read_data64, expected));
            shadow_reads = shadow_reads + 1;
            @(negedge clk);
            read_enable = 1'b0;
            @(posedge clk);
            #1;
            check(!read_valid64, "XLEN=64 read_valid failed to deassert");
        end
    endtask

    task automatic expect_read(
        input logic [11:0] address,
        input logic [1:0] warp,
        input logic [31:0] expected,
        input logic expected_error
    );
        begin
            @(negedge clk);
            read_enable = 1'b1;
            read_addr = address;
            read_wid = warp;
            @(posedge clk);
            #1;
            check(read_valid, "read_valid missing on registered response");
            check(read_data === expected, $sformatf(
                "CSR %03h data got=%08h expected=%08h", address, read_data, expected));
            check(read_error === expected_error, $sformatf(
                "CSR %03h error got=%b expected=%b", address, read_error, expected_error));
            shadow_reads = shadow_reads + 1;
            if (expected_error)
                shadow_errors = shadow_errors + 1;
            @(negedge clk);
            read_enable = 1'b0;
            @(posedge clk);
            #1;
            check(!read_valid, "read_valid failed to deassert after an idle read cycle");
        end
    endtask

    task automatic perform_write(
        input logic [11:0] address,
        input logic [1:0] warp,
        input logic [31:0] value,
        input logic legal
    );
        begin
            @(negedge clk);
            read_wid = warp;
            write_enable = 1'b1;
            write_addr = address;
            write_data = value;
            check(write_ready, "write_ready must be permanently asserted");
            @(posedge clk);
            #1;
            check(write_ready, "write_ready dropped after write edge");
            shadow_writes = shadow_writes + 1;
            if (!legal)
                shadow_errors = shadow_errors + 1;
            @(negedge clk);
            write_enable = 1'b0;
        end
    endtask

    task automatic check_back_to_back_reads;
        begin
            @(negedge clk);
            read_enable = 1'b1;
            read_wid = 2'd0;
            read_addr = 12'hF11;
            @(posedge clk);
            #1;
            check(read_valid && read_data == 32'h0000_002a && !read_error,
                  "first back-to-back read misaligned");
            shadow_reads = shadow_reads + 1;
            @(negedge clk);
            read_addr = 12'hF13;
            @(posedge clk);
            #1;
            check(read_valid && read_data == 32'h0000_004c && !read_error,
                  "second back-to-back read misaligned");
            shadow_reads = shadow_reads + 1;
            @(negedge clk);
            read_enable = 1'b0;
            @(posedge clk);
            #1;
            check(!read_valid, "back-to-back sequence left read_valid asserted");
            coverage[8] = 1'b1;
        end
    endtask

    task automatic check_same_cycle_read_write_old_value;
        begin
            perform_write(12'h340, 2'd0, 32'h1111_2222, 1'b1);
            @(negedge clk);
            read_enable = 1'b1;
            read_addr = 12'h340;
            read_wid = 2'd0;
            write_enable = 1'b1;
            write_addr = 12'h340;
            write_data = 32'h3333_4444;
            @(posedge clk);
            #1;
            check(read_valid && read_data == 32'h1111_2222 && !read_error,
                  "same-cycle CSR read did not return pre-write value");
            shadow_reads = shadow_reads + 1;
            shadow_writes = shadow_writes + 1;
            @(negedge clk);
            read_enable = 1'b0;
            write_enable = 1'b0;
            expect_read(12'h340, 2'd0, 32'h3333_4444, 1'b0);
            coverage[9] = 1'b1;
        end
    endtask

    initial begin
        repeat (3) @(posedge clk);
        @(negedge clk);
        reset = 1'b0;
        check(write_ready, "write_ready low after reset");
        check(!read_valid, "read_valid not reset");
        coverage[0] = 1'b1;

        // Reset state and complete read-only machine CSR map.
        expect_read(12'h340, 0, 32'h1020_3040, 0);
        expect_read(12'hF11, 0, 32'h0000_002a, 0);
        expect_read(12'hF12, 0, 32'h0000_003b, 0);
        expect_read(12'hF13, 0, 32'h0000_004c, 0);
        expect_read(12'hF14, 0, 32'h0000_0007, 0);
        expect_read(12'hF15, 0, 32'h0000_0000, 0);
        expect_read(12'h301, 0, 32'h4000_1100, 0);
        expect_read(12'h302, 0, 32'h0000_0000, 0);
        expect_read(12'h303, 0, 32'h0000_0000, 0);
        expect_read(12'h344, 0, 32'h0000_0000, 0);
        coverage[1] = 1'b1;

        // Every writable machine CSR, including alignment behavior.
        perform_write(12'h300, 0, 32'h1234_5678, 1);
        perform_write(12'h304, 0, 32'h0000_0880, 1);
        perform_write(12'h305, 0, 32'h1234_567b, 1);
        perform_write(12'h306, 0, 32'h0000_0005, 1);
        perform_write(12'h341, 0, 32'h8765_4321, 1);
        perform_write(12'h342, 0, 32'h8000_000b, 1);
        perform_write(12'h343, 0, 32'hfeed_cafe, 1);
        perform_write(12'h180, 0, 32'h8000_001f, 1);
        expect_read(12'h300, 0, 32'h1234_5678, 0);
        expect_read(12'h304, 0, 32'h0000_0880, 0);
        expect_read(12'h305, 0, 32'h1234_5678, 0);
        expect_read(12'h306, 0, 32'h0000_0005, 0);
        expect_read(12'h341, 0, 32'h8765_4320, 0);
        expect_read(12'h342, 0, 32'h8000_000b, 0);
        expect_read(12'h343, 0, 32'hfeed_cafe, 0);
        expect_read(12'h180, 0, 32'h8000_001f, 0);
        coverage[2] = 1'b1;

        // Per-warp scratch isolation.
        perform_write(12'hCCB, 2, 32'hcafe_babe, 1);
        perform_write(12'hCCB, 1, 32'h1357_9bdf, 1);
        expect_read(12'hCCB, 2, 32'hcafe_babe, 0);
        expect_read(12'hCCB, 1, 32'h1357_9bdf, 0);
        expect_read(12'hCCB, 3, 32'h0000_0000, 0);
        coverage[3] = 1'b1;

        // External counters and custom topology/state values.
        expect_read(12'hB00, 0, 32'h9abc_def0, 0);
        expect_read(12'hB80, 0, 32'h1234_5678, 0);
        expect_read(12'hB02, 0, 32'h89ab_cdef, 0);
        expect_read(12'hB82, 0, 32'h0123_4567, 0);
        expect_read(12'hCC0, 2, 32'd2, 0);
        expect_read(12'hCC1, 0, 32'd7, 0);
        expect_read(12'hCC2, 0, 32'h0000_00a5, 0);
        expect_read(12'hCC3, 0, 32'h0000_000b, 0);
        expect_read(12'hCC4, 0, 32'd8, 0);
        expect_read(12'hCC5, 0, 32'd4, 0);
        expect_read(12'hCC6, 0, 32'd6, 0);
        expect_read(12'hCC7, 0, 32'h8000_0000, 0);
        coverage[4] = 1'b1;

        // XLEN=64 uses the full counters, zero high-half aliases, and MXL=2.
        expect_read64(12'h301, 64'h8000_0000_0000_1100);
        expect_read64(12'hB00, 64'h1234_5678_9abc_def0);
        expect_read64(12'hB02, 64'h0123_4567_89ab_cdef);
        expect_read64(12'hB80, 64'h0000_0000_0000_0000);
        check(write_ready64, "XLEN=64 write_ready must be asserted");
        coverage[10] = 1'b1;

        // Invalid read and invalid/read-only write accounting.
        expect_read(12'hFFF, 0, 32'h0000_0000, 1);
        perform_write(12'hFFF, 0, 32'hffff_ffff, 0);
        perform_write(12'hF12, 0, 32'hffff_ffff, 0);
        expect_read(12'hF12, 0, 32'h0000_003b, 0);
        coverage[5] = 1'b1;

        // Counter CSRs expose pre-edge values and count their own reads.
        expect_read(12'hCC8, 0, 32'(shadow_reads), 0);
        expect_read(12'hCC9, 0, 32'(shadow_writes), 0);
        expect_read(12'hCCA, 0, 32'(shadow_errors), 0);
        coverage[6] = 1'b1;

        check_back_to_back_reads();
        check_same_cycle_read_write_old_value();

        // Reset must restore architectural state and counters.
        @(negedge clk);
        reset = 1'b1;
        @(posedge clk);
        #1;
        check(!read_valid && write_ready, "reset response outputs incorrect");
        @(negedge clk);
        reset = 1'b0;
        shadow_reads = 0;
        shadow_writes = 0;
        shadow_errors = 0;
        expect_read(12'h340, 0, 32'h1020_3040, 0);
        expect_read(12'h300, 0, 32'h0000_0000, 0);
        expect_read(12'hCCB, 2, 32'h0000_0000, 0);
        expect_read(12'hCC9, 0, 32'h0000_0000, 0);
        expect_read(12'hCCA, 0, 32'h0000_0000, 0);
        coverage[7] = 1'b1;

        check(&coverage, "one or more required CSR coverage groups were missed");
        if (errors != 0)
            $fatal(1, "VX_csr_data: %0d errors in %0d checks", errors, checks);
        $display("PASS VX_csr_data: %0d checks", checks);
        $finish;
    end

    initial begin
        #50000;
        $fatal(1, "VX_csr_data testbench timeout");
    end
endmodule

`default_nettype wire
