`timescale 1ns / 1ps
`default_nettype none

module VX_local_mem_tb;
    localparam integer DATAW      = 32;
    localparam integer SIZE_WORDS = 64;
    localparam integer NUM_REQS   = 4;
    localparam integer NUM_BANKS  = 4;
    localparam integer TAGW       = 8;
    localparam integer ADDRW      = $clog2(SIZE_WORDS);
    localparam integer BYTEENW    = DATAW / 8;

    logic clk;
    logic reset;
    logic [NUM_REQS-1:0] req_valid;
    logic [NUM_REQS-1:0] req_ready;
    logic [NUM_REQS-1:0] req_write;
    logic [NUM_REQS-1:0][ADDRW-1:0] req_addr;
    logic [NUM_REQS-1:0][BYTEENW-1:0] req_byteen;
    logic [NUM_REQS-1:0][DATAW-1:0] req_wdata;
    logic [NUM_REQS-1:0][TAGW-1:0] req_tag;
    logic [NUM_REQS-1:0] rsp_valid;
    logic [NUM_REQS-1:0] rsp_ready;
    logic [NUM_REQS-1:0][DATAW-1:0] rsp_rdata;
    logic [NUM_REQS-1:0][TAGW-1:0] rsp_tag;
    logic [31:0] perf_reads;
    logic [31:0] perf_writes;
    logic [31:0] perf_bank_stalls;
    logic [31:0] perf_rsp_stalls;
    integer errors;
    integer checks;
    integer timeout;

    VX_local_mem #(
        .DATAW(DATAW),
        .SIZE_WORDS(SIZE_WORDS),
        .NUM_REQS(NUM_REQS),
        .NUM_BANKS(NUM_BANKS),
        .TAGW(TAGW)
    ) dut (.*);

    always #5 clk = ~clk;

    task automatic check(input logic condition, input string message);
        begin
            checks = checks + 1;
            if (!condition) begin
                errors = errors + 1;
                $display("ERROR %0t: %s", $time, message);
            end
        end
    endtask

    task automatic clear_request(input integer port_no);
        begin
            req_valid[port_no]  = 1'b0;
            req_write[port_no]  = 1'b0;
            req_addr[port_no]   = '0;
            req_byteen[port_no] = '0;
            req_wdata[port_no]  = '0;
            req_tag[port_no]    = '0;
        end
    endtask

    task automatic write_word(
        input integer port_no,
        input logic [ADDRW-1:0] address,
        input logic [BYTEENW-1:0] byte_enable,
        input logic [DATAW-1:0] data
    );
        begin
            @(negedge clk);
            req_valid[port_no]  = 1'b1;
            req_write[port_no]  = 1'b1;
            req_addr[port_no]   = address;
            req_byteen[port_no] = byte_enable;
            req_wdata[port_no]  = data;
            timeout = 0;
            do begin
                @(posedge clk);
                timeout = timeout + 1;
                check(timeout < 20, "write request deadlocked");
            end while (!req_ready[port_no] && timeout < 20);
            @(negedge clk);
            clear_request(port_no);
        end
    endtask

    task automatic read_word(
        input integer port_no,
        input logic [ADDRW-1:0] address,
        input logic [TAGW-1:0] tag_value,
        input logic [DATAW-1:0] expected_data
    );
        begin
            @(negedge clk);
            req_valid[port_no] = 1'b1;
            req_write[port_no] = 1'b0;
            req_addr[port_no]  = address;
            req_tag[port_no]   = tag_value;
            timeout = 0;
            do begin
                @(posedge clk);
                timeout = timeout + 1;
                check(timeout < 20, "read request deadlocked");
            end while (!req_ready[port_no] && timeout < 20);
            #1;
            check(rsp_valid[port_no],
                  "read response must become valid one cycle after request");
            check(rsp_rdata[port_no] == expected_data,
                  "read response data mismatch");
            check(rsp_tag[port_no] == tag_value,
                  "read response tag mismatch");
            @(negedge clk);
            clear_request(port_no);
        end
    endtask

    task automatic test_parallel_banks;
        integer p;
        begin
            @(negedge clk);
            for (p = 0; p < NUM_REQS; p = p + 1) begin
                req_valid[p]  = 1'b1;
                req_write[p]  = 1'b1;
                req_addr[p]   = ADDRW'(p);
                req_byteen[p] = '1;
                req_wdata[p]  = 32'h1000_0000 + p;
            end
            @(posedge clk);
            #1;
            check(req_ready == 4'b1111,
                  "four requests to distinct banks must be accepted together");
            @(negedge clk);
            req_valid = '0;

            for (p = 0; p < NUM_REQS; p = p + 1)
                read_word(p, ADDRW'(p), TAGW'(8'h20 + p),
                          32'h1000_0000 + p);
        end
    endtask

    task automatic test_same_bank_collision;
        logic first0;
        logic first1;
        begin
            @(negedge clk);
            req_valid[0]  = 1'b1;
            req_write[0]  = 1'b1;
            req_addr[0]   = ADDRW'(8);
            req_byteen[0] = '1;
            req_wdata[0]  = 32'haaaa_0008;
            req_valid[1]  = 1'b1;
            req_write[1]  = 1'b1;
            req_addr[1]   = ADDRW'(12);
            req_byteen[1] = '1;
            req_wdata[1]  = 32'hbbbb_000c;
            #1;
            first0 = req_ready[0];
            first1 = req_ready[1];
            @(posedge clk);
            #1;
            check(first0 ^ first1,
                  "same-bank requests must serialize with one winner");
            check(perf_bank_stalls != 0,
                  "bank collision must increment stall counter");
            @(negedge clk);
            if (first0)
                clear_request(0);
            if (first1)
                clear_request(1);
            @(posedge clk);
            #1;
            check((first0 && req_ready[1]) || (first1 && req_ready[0]),
                  "losing bank request must be accepted next");
            @(negedge clk);
            clear_request(0);
            clear_request(1);
            read_word(0, ADDRW'(8), 8'h81, 32'haaaa_0008);
            read_word(1, ADDRW'(12), 8'hc1, 32'hbbbb_000c);
        end
    endtask

    task automatic test_byte_writes;
        begin
            write_word(2, ADDRW'(22), 4'b1111, 32'h1122_3344);
            write_word(2, ADDRW'(22), 4'b0101, 32'haa55_cc77);
            read_word(2, ADDRW'(22), 8'h5a, 32'h1155_3377);
        end
    endtask

    task automatic test_response_backpressure;
        logic [31:0] held_data;
        logic [7:0] held_tag;
        logic [31:0] stall_before;
        begin
            write_word(3, ADDRW'(31), 4'hf, 32'hfeed_cafe);
            rsp_ready[3] = 1'b0;
            stall_before = perf_rsp_stalls;
            @(negedge clk);
            req_valid[3] = 1'b1;
            req_write[3] = 1'b0;
            req_addr[3]  = ADDRW'(31);
            req_tag[3]   = 8'he3;
            #1;
            check(req_ready[3], "backpressured read request was not accepted");
            @(posedge clk);
            #1;
            check(rsp_valid[3], "read response timing changed under backpressure");
            held_data = rsp_rdata[3];
            held_tag  = rsp_tag[3];
            @(negedge clk);
            clear_request(3);
            repeat (3) begin
                @(posedge clk);
                #1;
                check(rsp_valid[3], "rsp_valid dropped while rsp_ready was low");
                check(rsp_rdata[3] == held_data,
                      "response data changed while stalled");
                check(rsp_tag[3] == held_tag,
                      "response tag changed while stalled");
            end
            check(held_data == 32'hfeed_cafe && held_tag == 8'he3,
                  "stalled response payload mismatch");
            check(perf_rsp_stalls >= stall_before + 3,
                  "response stall counter did not count backpressure");
            @(negedge clk);
            check(rsp_valid[3], "response disappeared before ready handshake");
            rsp_ready[3] = 1'b1;
            @(posedge clk);
            #1;
            check(!rsp_valid[3], "response did not retire after handshake");
        end
    endtask

    initial begin
        clk         = 1'b0;
        reset       = 1'b1;
        req_valid   = '0;
        req_write   = '0;
        req_addr    = '0;
        req_byteen  = '0;
        req_wdata   = '0;
        req_tag     = '0;
        rsp_ready   = '1;
        errors      = 0;
        checks      = 0;

        repeat (4) @(posedge clk);
        @(negedge clk);
        reset = 1'b0;
        check(req_ready == '0, "idle arbiters must not grant absent requests");
        check(rsp_valid == '0, "response valid state did not reset");
        check(perf_reads == 0 && perf_writes == 0
              && perf_bank_stalls == 0 && perf_rsp_stalls == 0,
              "performance counters must reset to zero");

        test_parallel_banks();
        test_same_bank_collision();
        test_byte_writes();
        test_response_backpressure();

        check(perf_reads >= 7, "read counter is too small");
        check(perf_writes >= 9, "write counter is too small");

        if (errors == 0) begin
            $display("PASS: VX_local_mem completed %0d checks", checks);
            $finish;
        end else begin
            $fatal(1, "VX_local_mem failed %0d of %0d checks", errors, checks);
        end
    end

    initial begin
        #20000;
        $fatal(1, "VX_local_mem timeout");
    end
endmodule

`default_nettype wire
