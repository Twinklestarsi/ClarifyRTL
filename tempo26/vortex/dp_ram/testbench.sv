`timescale 1ns/1ps
`default_nettype none

module VX_dp_ram_tb;
    logic clk = 1'b0;
    logic reset = 1'b1;
    logic write = 1'b0;
    logic read = 1'b0;
    logic [3:0] wren = '0;
    logic [3:0] waddr = '0;
    logic [3:0] raddr = '0;
    logic [31:0] wdata = '0;

    wire rvalid;
    wire [31:0] rdata;
    wire wf_rvalid;
    wire [31:0] wf_rdata;
    wire nc_rvalid;
    wire [31:0] nc_rdata;
    wire pipe_rvalid;
    wire [31:0] pipe_rdata;
    wire rr_rvalid;
    wire [31:0] rr_rdata;
    wire init_rvalid;
    wire [31:0] init_rdata;
    wire ar_rvalid;
    wire [31:0] ar_rdata;

    integer errors = 0;
    integer checks = 0;
    logic [8:0] coverage = '0;

    always #5 clk = ~clk;

    VX_dp_ram #(.DATAW(32), .SIZE(16), .RDW_MODE("R")) dut (
        .clk, .reset, .write, .wren, .waddr, .wdata, .read, .raddr,
        .rvalid, .rdata
    );
    VX_dp_ram #(.DATAW(32), .SIZE(16), .RDW_MODE("W")) dut_write_first (
        .clk, .reset, .write, .wren, .waddr, .wdata, .read, .raddr,
        .rvalid(wf_rvalid), .rdata(wf_rdata)
    );
    VX_dp_ram #(.DATAW(32), .SIZE(16), .RDW_MODE("N")) dut_no_change (
        .clk, .reset, .write, .wren, .waddr, .wdata, .read, .raddr,
        .rvalid(nc_rvalid), .rdata(nc_rdata)
    );
    VX_dp_ram #(
        .DATAW(32), .SIZE(16), .READ_LATENCY(2), .OUT_REG(1), .RDW_MODE("R")
    ) dut_pipeline (
        .clk, .reset, .write, .wren, .waddr, .wdata, .read, .raddr,
        .rvalid(pipe_rvalid), .rdata(pipe_rdata)
    );
    VX_dp_ram #(
        .DATAW(32), .SIZE(16), .RESET_RAM(1), .INIT_VALUE(32'h55aa_aa55)
    ) dut_reset_ram (
        .clk, .reset, .write, .wren, .waddr, .wdata, .read, .raddr,
        .rvalid(rr_rvalid), .rdata(rr_rdata)
    );
    VX_dp_ram #(
        .DATAW(32), .SIZE(16), .INIT_ENABLE(1), .INIT_VALUE(32'ha5a5_5a5a)
    ) dut_initialized (
        .clk, .reset, .write, .wren, .waddr, .wdata, .read, .raddr,
        .rvalid(init_rvalid), .rdata(init_rdata)
    );
    VX_dp_ram #(
        .DATAW(32), .SIZE(16), .RADDR_REG(1), .INIT_ENABLE(1),
        .INIT_VALUE(32'h0bad_f00d)
    ) dut_address_registered (
        .clk, .reset, .write, .wren, .waddr, .wdata, .read, .raddr,
        .rvalid(ar_rvalid), .rdata(ar_rdata)
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

    task automatic do_write(
        input logic [3:0] address,
        input logic [3:0] enables,
        input logic [31:0] data
    );
        begin
            @(negedge clk);
            write = 1'b1;
            waddr = address;
            wren = enables;
            wdata = data;
            @(posedge clk);
            #1;
            @(negedge clk);
            write = 1'b0;
            wren = '0;
        end
    endtask

    task automatic do_base_read(
        input logic [3:0] address,
        input logic [31:0] expected
    );
        begin
            @(negedge clk);
            read = 1'b1;
            raddr = address;
            @(posedge clk);
            #1;
            check(rvalid, "default read response valid missing");
            check(rdata === expected, $sformatf(
                "default RAM address %0d got=%08h expected=%08h", address, rdata, expected));
            @(negedge clk);
            read = 1'b0;
            @(posedge clk);
            #1;
            check(!rvalid, "default rvalid did not deassert after idle read cycle");
        end
    endtask

    task automatic check_back_to_back;
        begin
            @(negedge clk);
            read = 1'b1;
            raddr = 4'd3;
            @(posedge clk);
            #1;
            check(rvalid && rdata == 32'h11bb_33dd,
                  "first consecutive read was misaligned");
            @(negedge clk);
            raddr = 4'd7;
            @(posedge clk);
            #1;
            check(rvalid && rdata == 32'hdead_beef,
                  "second consecutive read was misaligned");
            @(negedge clk);
            read = 1'b0;
            @(posedge clk);
            #1;
            check(!rvalid, "consecutive read sequence left rvalid high");
            coverage[2] = 1'b1;
        end
    endtask

    task automatic check_three_stage_pipeline;
        begin
            // Drain responses created in this instance by earlier shared reads.
            read = 1'b0;
            repeat (4) @(posedge clk);
            @(negedge clk);
            read = 1'b1;
            raddr = 4'd7;
            @(posedge clk);
            #1;
            check(!pipe_rvalid, "deep pipeline responded on acceptance edge");
            @(negedge clk);
            read = 1'b0;
            @(posedge clk);
            #1;
            check(!pipe_rvalid, "deep pipeline responded one edge too early");
            @(posedge clk);
            #1;
            check(pipe_rvalid && pipe_rdata == 32'hdead_beef,
                  "READ_LATENCY+OUT_REG response timing/data mismatch");
            @(posedge clk);
            #1;
            check(!pipe_rvalid, "deep pipeline valid width was not one cycle");
            coverage[6] = 1'b1;
        end
    endtask

    initial begin
        repeat (3) @(posedge clk);
        @(negedge clk);
        reset = 1'b0;
        coverage[0] = 1'b1;

        // RESET_RAM and INIT_ENABLE contents are independently observable.
        @(negedge clk);
        read = 1'b1;
        raddr = 4'd12;
        @(posedge clk);
        #1;
        check(rr_rvalid && rr_rdata == 32'h55aa_aa55,
              "RESET_RAM did not fill memory with INIT_VALUE");
        check(init_rvalid && init_rdata == 32'ha5a5_5a5a,
              "INIT_ENABLE/INIT_VALUE initialization mismatch");
        @(negedge clk);
        read = 1'b0;
        coverage[7] = 1'b1;

        do_write(4'd3, 4'b1111, 32'h1122_3344);
        do_base_read(4'd3, 32'h1122_3344);
        coverage[1] = 1'b1;

        // Slice enables update bytes 0 and 2 only; zero mask changes nothing.
        do_write(4'd3, 4'b0101, 32'haabb_ccdd);
        do_base_read(4'd3, 32'h11bb_33dd);
        do_write(4'd3, 4'b0000, 32'hffff_ffff);
        do_base_read(4'd3, 32'h11bb_33dd);
        coverage[3] = 1'b1;

        do_write(4'd7, 4'b1111, 32'hdead_beef);
        check_back_to_back();

        // Prime no-change hold data with address seven.
        do_base_read(4'd7, 32'hdead_beef);

        // Same-address partial collision distinguishes R, W, and N modes.
        @(negedge clk);
        read = 1'b1;
        write = 1'b1;
        raddr = 4'd3;
        waddr = 4'd3;
        wren = 4'b0011;
        wdata = 32'h0102_0304;
        @(posedge clk);
        #1;
        check(rvalid && rdata == 32'h11bb_33dd,
              "read-first collision did not return old word");
        check(wf_rvalid && wf_rdata == 32'h11bb_0304,
              "write-first collision did not return byte-merged word");
        check(nc_rvalid && nc_rdata == 32'hdead_beef,
              "no-change collision did not return prior sampled read");
        @(negedge clk);
        read = 1'b0;
        write = 1'b0;
        wren = '0;
        do_base_read(4'd3, 32'h11bb_0304);
        coverage[4] = 1'b1;
        coverage[5] = 1'b1;

        check_three_stage_pipeline();

        // Registered-address option uses the address held before the read edge.
        @(negedge clk);
        read = 1'b1;
        raddr = 4'd9;
        @(posedge clk);
        #1;
        check(ar_rvalid && ar_rdata == 32'hdead_beef,
              "registered-address first access did not use prior registered address");
        @(negedge clk);
        raddr = 4'd9;
        @(posedge clk);
        #1;
        check(ar_rvalid && ar_rdata == 32'h0bad_f00d,
              "registered-address preloaded access mismatch");
        @(negedge clk);
        read = 1'b0;
        coverage[8] = 1'b1;

        check(&coverage, "one or more required RAM coverage groups were missed");
        if (errors != 0)
            $fatal(1, "VX_dp_ram: %0d errors in %0d checks", errors, checks);
        $display("PASS VX_dp_ram: %0d checks", checks);
        $finish;
    end

    initial begin
        #50000;
        $fatal(1, "VX_dp_ram testbench timeout");
    end
endmodule

`default_nettype wire
